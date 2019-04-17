from enum import Enum
import tensorflow as tf


class TFExampleCodec(object):
    def __init__(self, feature_schema):
        self._f_desc = {}
        self._f_name2type = dict(feature_schema)
        for f_name, f_type in self._f_name2type.items():
            if f_type == tf.string:
                self._f_desc[f_name] = tf.FixedLenFeature(
                    [], tf.string, default_value=""
                )
            elif f_type in (tf.int64, tf.float32):
                self._f_desc[f_name] = tf.FixedLenFeature(
                    [], f_type, default_value=f_type.as_numpy_dtype(0)
                )
            else:
                raise ValueError(
                    "not supported tensorflow data type: " + f_type
                )

    def encode(self, example):
        f_dict = {}
        for f_name, f_value in example:
            f_type = self._f_name2type[f_name]
            if f_type == tf.string:
                f_dict[f_name] = tf.train.Feature(
                    bytes_list=tf.train.BytesList(value=[f_value])
                )
            elif f_type == tf.float32:
                f_dict[f_name] = tf.train.Feature(
                    float_list=tf.train.FloatList(value=[f_value])
                )
            elif f_type == tf.int64:
                f_dict[f_name] = tf.train.Feature(
                    int64_list=tf.train.Int64List(value=[f_value])
                )
            else:
                raise ValueError(
                    "not supported tensorflow data type: " + f_type
                )

        example = tf.train.Example(features=tf.train.Features(feature=f_dict))
        return example.SerializeToString()

    def decode(self, raw):
        return tf.parse_single_example(raw, self._f_desc)