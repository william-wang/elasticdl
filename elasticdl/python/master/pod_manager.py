# Copyright 2020 The ElasticDL Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import copy
import itertools
import math
import os
import threading
import time
from collections import Counter, namedtuple

from kubernetes.client import V1EnvVar

from elasticdl.python.common import k8s_client as k8s
from elasticdl.python.common.constants import (
    PodManagerStatus,
    PodStatus,
    WorkerEnv,
)
from elasticdl.python.common.k8s_client import PodType
from elasticdl.python.common.log_utils import default_logger as logger
from elasticdl.python.common.model_utils import get_dict_from_params_str
from elasticdl.python.master.pod_event_callbacks import ClusterContext, PodInfo
from elasticdl_client.common.args import parse_envs
from elasticdl_client.common.constants import (
    BashCommandTemplate,
    ClusterSpecConfig,
)
from elasticdl_client.common.k8s_client import (
    ELASTICDL_REPLICA_INDEX_KEY,
    ELASTICDL_REPLICA_TYPE_KEY,
)

_SERVICE_ADDR_SEP = ","


def _get_addrs(num_addrs, addr_get_fn):
    """
    Get `num_addrs` addresses and then concatenate
    them to a comma separated string.
    """
    addrs = []
    for addr_id in range(num_addrs):
        addrs.append(addr_get_fn(addr_id))
    return _SERVICE_ADDR_SEP.join(addrs)


def _is_float_str(str_number):
    if not str_number:
        return False
    try:
        float(str_number)
        return True
    except ValueError:
        return False


def _parse_worker_pod_priority(num_workers, worker_pod_priority):
    res = {}
    if _is_float_str(worker_pod_priority):
        fraction = float(worker_pod_priority)
        high_count = math.ceil(num_workers * fraction)
        for i in range(num_workers):
            if i < high_count:
                res[i] = "high"
            else:
                res[i] = "low"
    elif worker_pod_priority in [None, "", "high", "low"]:
        for i in range(num_workers):
            res[i] = worker_pod_priority
    else:
        raise ValueError(
            "Not support priority = {}, please set priority = "
            "high/low/a fraction value.".format(worker_pod_priority)
        )
    return res


def _should_relaunch_killed_pod(evt_obj):
    """
    Check whether to relaunch the failed pod according to the kubernetes event.
    For the killed pods, we will try to relaunch them except the
    OOM ones.
    """
    return (
        evt_obj.status.container_statuses
        and evt_obj.status.container_statuses[0].state.terminated
        and evt_obj.status.container_statuses[0].state.terminated.exit_code
        == 137
        and evt_obj.status.container_statuses[0].state.terminated.reason
        != "OOMKilled"
    )


def _get_start_running_time_stamp(pod_status_obj):
    if (
        pod_status_obj.container_statuses
        and pod_status_obj.container_statuses[0].state
        and pod_status_obj.container_statuses[0].state.running
    ):
        return pod_status_obj.container_statuses[0].state.running.started_at

    return None


def get_image_cluster_spec(cluster_spec):
    if cluster_spec:
        filename = os.path.basename(cluster_spec)
        image_cluster_spec = os.path.join(
            ClusterSpecConfig.CLUSTER_SPEC_DIR, filename
        )
        return image_cluster_spec
    return cluster_spec


def create_pod_manager(args):
    pod_manager = None

    master_ip = os.getenv("MY_POD_IP", "localhost")
    master_addr = "%s:%d" % (master_ip, args.port)
    if args.num_workers:
        assert args.worker_image, "Worker image cannot be empty"

        env_dict = parse_envs(args.envs)
        env = []
        for key in env_dict:
            env.append(V1EnvVar(name=key, value=env_dict[key]))
        env.append(V1EnvVar(name=WorkerEnv.MASTER_ADDR, value=master_addr))
        env.append(
            V1EnvVar(name=WorkerEnv.WORKER_NUM, value=str(args.num_workers))
        )

        kwargs = get_dict_from_params_str(args.aux_params)
        disable_relaunch = kwargs.get("disable_relaunch", False)
        cluster_spec = get_image_cluster_spec(args.cluster_spec)

        pod_manager = PodManager(
            job_name=args.job_name,
            image_name=args.worker_image,
            namespace=args.namespace,
            num_workers=args.num_workers,
            worker_resource_request=args.worker_resource_request,
            worker_resource_limit=args.worker_resource_limit,
            worker_pod_priority=args.worker_pod_priority,
            num_ps=args.num_ps_pods,
            ps_resource_request=args.ps_resource_request,
            ps_resource_limit=args.ps_resource_limit,
            ps_pod_priority=args.ps_pod_priority,
            volume=args.volume,
            image_pull_policy=args.image_pull_policy,
            restart_policy=args.restart_policy,
            cluster_spec=cluster_spec,
            cluster_spec_json=args.cluster_spec_json,
            envs=env,
            disable_relaunch=disable_relaunch,
            log_file_path=args.log_file_path,
        )

    return pod_manager


PodStateFlow = namedtuple(
    "PodStateFlow",
    ("from_status", "to_status", "event_type", "phase", "should_relaunch"),
)

"""
The DAG for the state machine is in the issue
https://github.com/sql-machine-learning/elasticdl/issues/2395#issue-753964852
"""
POD_STATE_FLOWS = [
    PodStateFlow(
        from_status=PodStatus.INITIAL,
        to_status=PodStatus.PENDING,
        event_type="ADDED",
        phase="Pending",
        should_relaunch=False,
    ),
    PodStateFlow(
        from_status=PodStatus.INITIAL,
        to_status=PodStatus.RUNNING,
        event_type="ADDED",
        phase="Running",
        should_relaunch=False,
    ),
    PodStateFlow(
        from_status=PodStatus.PENDING,
        to_status=PodStatus.RUNNING,
        event_type="MODIFIED",
        phase="Running",
        should_relaunch=False,
    ),
    PodStateFlow(
        from_status=PodStatus.RUNNING,
        to_status=PodStatus.SUCCEEDED,
        event_type="MODIFIED",
        phase="Succeeded",
        should_relaunch=False,
    ),
    PodStateFlow(
        from_status=PodStatus.RUNNING,
        to_status=PodStatus.FAILED,
        event_type="MODIFIED",
        phase="Failed",
        should_relaunch=True,
    ),
    PodStateFlow(
        from_status=PodStatus.PENDING,
        to_status=PodStatus.DELETED,
        event_type="DELETED",
        phase=None,
        should_relaunch=True,
    ),
    PodStateFlow(
        from_status=PodStatus.RUNNING,
        to_status=PodStatus.DELETED,
        event_type="DELETED",
        phase=None,
        should_relaunch=True,
    ),
    PodStateFlow(
        from_status=PodStatus.SUCCEEDED,
        to_status=PodStatus.DELETED,
        event_type="DELETED",
        phase=None,
        should_relaunch=False,
    ),
    PodStateFlow(
        from_status=PodStatus.FAILED,
        to_status=PodStatus.DELETED,
        event_type="DELETED",
        phase=None,
        should_relaunch=False,
    ),
]


class PodManager(object):
    def __init__(
        self,
        num_workers=1,
        worker_resource_request="cpu=1,memory=4096Mi",
        worker_resource_limit="cpu=1,memory=4096Mi",
        worker_pod_priority=None,
        num_ps=0,
        ps_resource_request="cpu=1,memory=4096Mi",
        ps_resource_limit="cpu=1,memory=4096Mi",
        ps_pod_priority=None,
        volume=None,
        image_pull_policy=None,
        restart_policy="Never",
        envs=None,
        disable_relaunch=False,
        log_file_path=None,
        **kwargs
    ):
        self._num_workers = num_workers
        self._worker_resource_request = worker_resource_request
        self._worker_resource_limit = worker_resource_limit
        self._worker_pod_priority = _parse_worker_pod_priority(
            self._num_workers, worker_pod_priority
        )

        self._num_ps = num_ps
        self._ps_resource_request = ps_resource_request
        self._ps_resource_limit = ps_resource_limit
        self._ps_pod_priority = ps_pod_priority

        self._restart_policy = restart_policy
        self._volume = volume
        self._image_pull_policy = image_pull_policy
        self._envs = envs
        self._next_worker_id_fn = itertools.count().__next__
        self._log_file_path = log_file_path

        # Protects followed variables, which are accessed from event_cb.
        self._lock = threading.Lock()

        self._init_pod_status()

        if disable_relaunch:
            self._k8s_client = k8s.Client(**kwargs)
        else:
            self._k8s_client = k8s.Client(
                event_callback=self._event_cb,
                periodic_call_func=self._process_worker,
                **kwargs
            )
        self._ps_addrs = _get_addrs(
            self._num_ps, self._k8s_client.get_ps_service_address
        )
        self._worker_addrs = []
        self._worker_command = None
        self._worker_args = None
        self._ps_command = None
        self._ps_args = None
        self._pod_event_callbacks = []

    def set_up(
        self,
        worker_command=None,
        worker_args=None,
        ps_command=None,
        ps_args=None,
    ):
        self._worker_command = worker_command
        self._worker_args = worker_args
        self._ps_command = ps_command
        self._ps_args = ps_args

    def start(self):
        self._k8s_client.start_watch_events()
        self.update_status(PodManagerStatus.PENDING)
        if self._num_ps > 0:
            logger.info("num ps pods : {}".format(self._num_ps))
            self.start_parameter_servers()
        self.start_workers()
        self.update_status(PodManagerStatus.RUNNING)

    def add_pod_event_callback(self, pod_event_callback):
        self._pod_event_callbacks.append(pod_event_callback)

    def _init_pod_status(self):
        # _pod_info_cache is a dict. The key is the PodType. The value
        # is also a dict  mapping from pod_name to PodInfo object.
        self._pod_info_cache = {PodType.PS: {}, PodType.WORKER: {}}

        # worker ids for the pods which are not created.
        # We will try multiple times in the background to create the pod
        # using the id in the list until success.
        self._not_created_worker_id = []

        self._relaunch_worker = True

    def _process_worker(self):
        need_process = True
        while need_process and self._not_created_worker_id:
            worker_id = self._not_created_worker_id.pop()
            # Try to create a worker pod with id as worker_id
            need_process = self._start_worker(worker_id)

    def _start_worker(self, worker_id):
        logger.info("Starting worker: %d" % worker_id)
        bash_command = self._worker_args[1]
        if self._ps_addrs:
            bash_command += " --ps_addrs {}".format(self._ps_addrs)
        if self._log_file_path:
            bash_command += BashCommandTemplate.REDIRECTION.format(
                self._log_file_path
            )
        for extra_arg in self._worker_args[2:]:
            bash_command += " {}".format(extra_arg)
        worker_args = [self._worker_args[0], bash_command]
        envs = copy.deepcopy(self._envs)
        envs.append(V1EnvVar(name=WorkerEnv.WORKER_ID, value=str(worker_id)))
        with self._lock:
            pod = self._k8s_client.create_worker(
                worker_id=worker_id,
                resource_requests=self._worker_resource_request,
                resource_limits=self._worker_resource_limit,
                pod_priority=self._worker_pod_priority[worker_id],
                termination_period=1,
                volume=self._volume,
                image_pull_policy=self._image_pull_policy,
                command=self._worker_command,
                args=worker_args,
                restart_policy=self._restart_policy,
                ps_addrs=self._ps_addrs,
                envs=envs,
            )
            if pod is None:
                self._not_created_worker_id.append(worker_id)
                return False

            return True

    def _start_ps(self, ps_id):
        logger.info("Starting PS: %d" % ps_id)
        bash_command = self._ps_args[1]
        bash_command += " --ps_id {}".format(ps_id)
        if self._log_file_path:
            bash_command += BashCommandTemplate.REDIRECTION.format(
                self._log_file_path
            )
        ps_args = [self._ps_args[0], bash_command]
        while True:
            with self._lock:
                pod = self._create_ps_pod(ps_id, ps_args)
                if pod:
                    self._k8s_client.create_ps_service(ps_id)
                    break
            # TODO: should we fail the job when ps pods fail to
            #       create for a long time?
            logger.error(
                "Creating PS fails and will try again."
                "ps_id: {}, ps_args: {}.".format(ps_id, ps_args)
            )
            time.sleep(15)

    def _create_ps_pod(self, ps_id, ps_args):
        return self._k8s_client.create_ps(
            ps_id=ps_id,
            resource_requests=self._ps_resource_request,
            resource_limits=self._ps_resource_limit,
            pod_priority=self._ps_pod_priority,
            volume=self._volume,
            image_pull_policy=self._image_pull_policy,
            command=self._ps_command,
            args=ps_args,
            restart_policy=self._restart_policy,
            envs=copy.deepcopy(self._envs),
        )

    def update_status(self, status):
        master_name = self._k8s_client.get_master_pod_name()
        self._k8s_client.patch_labels_to_pod(
            master_name, labels_dict={"status": status}
        )

    def start_workers(self):
        for _ in range(self._num_workers):
            self._start_worker(self._next_worker_id_fn())

    def start_parameter_servers(self):
        for i in range(self._num_ps):
            self._start_ps(i)

    def _remove_worker(self, worker_id):
        logger.info("Removing worker: %d", worker_id)
        with self._lock:
            if worker_id not in [
                pod_info.id
                for pod_info in self._pod_info_cache[PodType.WORKER].values()
                if pod_info.status != PodStatus.DELETED
            ]:
                logger.error("Unknown deletable worker id: %s" % worker_id)
                return

        # TODO: change _k8s_client to accept pod name instead of worker id.
        self._k8s_client.delete_worker(worker_id)

    def _remove_parameter_server(self, ps_id):
        logger.info("Removing PS: %d", ps_id)
        with self._lock:
            if ps_id not in [
                pod_info.id
                for pod_info in self._pod_info_cache[PodType.PS].values()
                if pod_info.status != PodStatus.DELETED
            ]:
                logger.error("Unknown deletable PS id: %s" % ps_id)
                return

        self._k8s_client.delete_ps(ps_id)

    def stop_relaunch_and_remove_pods(self, pod_type):
        if pod_type == PodType.WORKER:
            self._relaunch_worker = False
        with self._lock:
            for pod_info in self._pod_info_cache[pod_type].values():
                if pod_info.status != PodStatus.DELETED:
                    self._k8s_client.delete_pod(pod_info.name)

    def get_pod_counter(self, pod_type):
        with self._lock:
            return Counter(
                [
                    pod_info.status
                    for pod_info in self._pod_info_cache[pod_type].values()
                ]
            )

    def _event_cb(self, event):
        evt_obj = event.get("object")
        evt_type = event.get("type")
        if not evt_obj or not evt_type:
            logger.error("Event doesn't have object or type: %s" % event)
            return

        if evt_obj.kind != "Pod":
            # We only care about pod related events
            return

        pod_name = evt_obj.metadata.name
        pod_ip = evt_obj.status.pod_ip
        phase = evt_obj.status.phase
        pod_start_time = _get_start_running_time_stamp(evt_obj.status)
        pod_type = evt_obj.metadata.labels[ELASTICDL_REPLICA_TYPE_KEY]

        if pod_type == PodType.MASTER:
            # No need to care about master pod
            return

        pod_id = int(evt_obj.metadata.labels[ELASTICDL_REPLICA_INDEX_KEY])

        # For the given worker id, check whether it meet
        # the state change condition
        with self._lock:
            pod_state = PodStatus.INITIAL
            if pod_name in self._pod_info_cache[pod_type]:
                pod_state = self._pod_info_cache[pod_type][pod_name].status
            matched_pod_state_flow = PodManager.get_pod_state_flow(
                pod_state, evt_type, phase
            )
            # If there is no matched state change, return directly
            if matched_pod_state_flow is None:
                return

            # Update the pod status in cache
            new_status = matched_pod_state_flow.to_status
            pod_info = PodInfo(
                type=pod_type,
                id=pod_id,
                name=pod_name,
                ip=pod_ip,
                status=new_status,
                start_time=pod_start_time,
            )
            self._pod_info_cache[pod_type][pod_name] = pod_info

        cluster_context = ClusterContext(pod_manager=self)
        should_relaunch = (
            pod_type == PodType.WORKER
            and matched_pod_state_flow.should_relaunch
            and self._relaunch_worker
        )

        if matched_pod_state_flow.to_status == PodStatus.RUNNING:
            [
                callback.on_pod_started(pod_info, cluster_context)
                for callback in self._pod_event_callbacks
            ]
        elif matched_pod_state_flow.to_status == PodStatus.SUCCEEDED:
            [
                callback.on_pod_succeeded(pod_info, cluster_context)
                for callback in self._pod_event_callbacks
            ]
        elif matched_pod_state_flow.to_status == PodStatus.FAILED:
            [
                callback.on_pod_failed(pod_info, cluster_context)
                for callback in self._pod_event_callbacks
            ]
            should_relaunch = should_relaunch and _should_relaunch_killed_pod(
                evt_obj=evt_obj
            )
        elif matched_pod_state_flow.to_status == PodStatus.DELETED:
            [
                callback.on_pod_deleted(pod_info, cluster_context)
                for callback in self._pod_event_callbacks
            ]

        if should_relaunch:
            logger.info("Relaunch the worker: {}".format(pod_name))

            new_worker_id = self._next_worker_id_fn()
            with self._lock:
                self._worker_pod_priority[
                    new_worker_id
                ] = self._worker_pod_priority[pod_id]
            self._start_worker(new_worker_id)

    @staticmethod
    def get_pod_state_flow(from_status, event_type, phase):
        for pod_state_flow in POD_STATE_FLOWS:
            if (
                from_status == pod_state_flow.from_status
                and event_type == pod_state_flow.event_type
                and (
                    pod_state_flow.phase is None
                    or phase == pod_state_flow.phase
                )
            ):
                return pod_state_flow

        return None

    @property
    def all_workers_exited(self):
        with self._lock:
            all_exited = all(
                [
                    pod_info.status
                    in [
                        PodStatus.SUCCEEDED,
                        PodStatus.FAILED,
                        PodStatus.DELETED,
                    ]
                    for pod_info in self._pod_info_cache[
                        PodType.WORKER
                    ].values()
                ]
            )

        return all_exited

    def get_alive_workers(self):
        with self._lock:
            return [
                pod_info
                for pod_info in self._pod_info_cache[PodType.WORKER].values()
                if pod_info.status == PodStatus.RUNNING
            ]

    def get_alive_worker_name_addr(self):
        alive_workers = self.get_alive_workers()
        alive_workers.sort(key=lambda pod_info: pod_info.start_time)

        return [(info.name, info.ip) for info in alive_workers]

    def get_worker_pod_ip(self, worker_id):
        with self._lock:
            for pod_info in self._pod_info_cache[PodType.WORKER].values():
                if pod_info.id == worker_id:
                    return pod_info.ip

        return None

    def get_pod_infos(self, pod_type, pod_statuses):
        with self._lock:
            return [
                pod_info
                for pod_info in self._pod_info_cache[pod_type].values()
                if pod_info.status in pod_statuses
            ]

    @property
    def ps_addrs(self):
        return self._ps_addrs
