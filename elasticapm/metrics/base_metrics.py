#  BSD 3-Clause License
#
#  Copyright (c) 2019, Elasticsearch BV
#  All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#  * Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
#  FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
#  DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#  SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#  CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#  OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import logging
import threading
import time
from collections import defaultdict

from elasticapm.conf import constants
from elasticapm.utils import compat, is_master_process
from elasticapm.utils.module_import import import_string
from elasticapm.utils.threading import IntervalTimer

logger = logging.getLogger("elasticapm.metrics")


class MetricsRegistry(object):
    def __init__(self, collect_interval, queue_func, tags=None, ignore_patterns=None):
        """
        Creates a new metric registry

        :param collect_interval: the interval to collect metrics from registered metric sets
        :param queue_func: the function to call with the collected metrics
        :param tags:
        """
        self._collect_interval = collect_interval
        self._queue_func = queue_func
        self._metricsets = {}
        self._tags = tags or {}
        self._collect_timer = None
        self._ignore_patterns = ignore_patterns or ()
        if self._collect_interval:
            # we only start the thread if we are not in a uwsgi master process
            if not is_master_process():
                self._start_collect_timer()
            else:
                # If we _are_ in a uwsgi master process, we use the postfork hook to start the thread after the fork
                compat.postfork(lambda: self._start_collect_timer())

    def register(self, class_path):
        """
        Register a new metric set
        :param class_path: a string with the import path of the metricset class
        """
        if class_path in self._metricsets:
            return
        else:
            try:
                class_obj = import_string(class_path)
                self._metricsets[class_path] = class_obj(self)
            except ImportError as e:
                logger.warning("Could not register %s metricset: %s", class_path, compat.text_type(e))

    def collect(self):
        """
        Collect metrics from all registered metric sets and queues them for sending
        :return:
        """
        logger.debug("Collecting metrics")

        for name, metricset in compat.iteritems(self._metricsets):
            for data in metricset.collect():
                self._queue_func(constants.METRICSET, data)

    def _start_collect_timer(self, timeout=None):
        timeout = timeout or self._collect_interval
        self._collect_timer = IntervalTimer(self.collect, timeout, name="eapm metrics collect timer", daemon=True)
        logger.debug("Starting metrics collect timer")
        self._collect_timer.start()

    def _stop_collect_timer(self):
        if self._collect_timer:
            logger.debug("Cancelling collect timer")
            self._collect_timer.cancel()


class MetricsSet(object):
    def __init__(self, registry):
        self._lock = threading.Lock()
        self._counters = {}
        self._gauges = {}
        self._registry = registry

    def counter(self, name, **labels):
        """
        Returns an existing or creates and returns a new counter
        :param name: name of the counter
        :param labels: a flat key/value map of labels
        :return: the counter object
        """
        labels = self._labels_to_key(labels)
        key = (name, labels)
        with self._lock:
            if key not in self._counters:
                if self._registry._ignore_patterns and any(
                    pattern.match(name) for pattern in self._registry._ignore_patterns
                ):
                    counter = noop_metric
                else:
                    counter = Counter(name)
                self._counters[key] = counter
            return self._counters[key]

    def gauge(self, name, **labels):
        """
        Returns an existing or creates and returns a new gauge
        :param name: name of the gauge
        :return: the gauge object
        """
        labels = self._labels_to_key(labels)
        key = (name, labels)
        with self._lock:
            if key not in self._gauges:
                if self._registry._ignore_patterns and any(
                    pattern.match(name) for pattern in self._registry._ignore_patterns
                ):
                    gauge = noop_metric
                else:
                    gauge = Gauge(name)
                self._gauges[key] = gauge
            return self._gauges[key]

    def collect(self):
        """
        Collects all metrics attached to this metricset, and returns it as a generator
        with one or more elements. More than one element is returned if labels are used.

        The format of the return value should be

            {
                "samples": {"metric.name": {"value": some_float}, ...},
                "timestamp": unix epoch in microsecond precision
            }
        """
        self.before_collect()
        timestamp = int(time.time() * 1000000)
        samples = defaultdict(dict)
        if self._counters:
            for (name, labels), c in compat.iteritems(self._counters):
                if c is not noop_metric:
                    samples[labels].update({name: {"value": c.val}})
        if self._gauges:
            for (name, labels), g in compat.iteritems(self._gauges):
                if g is not noop_metric:
                    samples[labels].update({name: {"value": g.val}})
        if samples:
            for labels, sample in compat.iteritems(samples):
                result = {"samples": sample, "timestamp": timestamp}
                if labels:
                    result["tags"] = {k: v for k, v in labels}
                yield result

    def before_collect(self):
        """
        A method that is called right before collection. Can be used to gather metrics.
        :return:
        """
        pass

    def _labels_to_key(self, labels):
        return tuple((k, compat.text_type(v)) for k, v in sorted(compat.iteritems(labels)))


class Counter(object):
    __slots__ = ("label", "_lock", "_initial_value", "_val")

    def __init__(self, label, initial_value=0):
        """
        Creates a new counter
        :param label: label of the counter
        :param initial_value: initial value of the counter, defaults to 0
        """
        self.label = label
        self._lock = threading.Lock()
        self._val = self._initial_value = initial_value

    def inc(self, delta=1):
        """
        Increments the counter. If no delta is provided, it is incremented by one
        :param delta: the amount to increment the counter by
        :returns the counter itself
        """
        with self._lock:
            self._val += delta
        return self

    def dec(self, delta=1):
        """
        Decrements the counter. If no delta is provided, it is decremented by one
        :param delta: the amount to decrement the counter by
        :returns the counter itself
        """
        with self._lock:
            self._val -= delta
        return self

    def reset(self):
        """
        Reset the counter to the initial value
        :returns the counter itself
        """
        with self._lock:
            self._val = self._initial_value
        return self

    @property
    def val(self):
        """Returns the current value of the counter"""
        return self._val


class Gauge(object):
    __slots__ = ("label", "_val")

    def __init__(self, label):
        """
        Creates a new gauge
        :param label: label of the gauge
        """
        self.label = label
        self._val = None

    @property
    def val(self):
        return self._val

    @val.setter
    def val(self, value):
        self._val = value


class NoopMetric(object):
    """
    A no-op metric that implements the "interface" of both Counter and Gauge.

    Note that even when using a no-op metric, the value itself will still be calculated.
    """

    def __init__(self, label, initial_value=0):
        return

    @property
    def val(self):
        return None

    @val.setter
    def val(self, value):
        return

    def inc(self, delta=1):
        return

    def dec(self, delta=-1):
        return

    def reset(self):
        return


noop_metric = NoopMetric("noop")