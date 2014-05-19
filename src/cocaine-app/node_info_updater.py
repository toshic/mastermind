# -*- coding: utf-8 -*-
from contextlib import contextmanager
import functools
import json
import sys
import threading
import time
import traceback

from cocaine.services import Service
import elliptics

import balancer
import balancelogicadapter as bla
from compat import EllAsyncResult, EllReadResult, EllLookupResult
from config import config
import errors
import keys
import timed_queue
import storage


GROUPS_META_UPDATE_TASK_ID = 'update_symms_for_groups'
COUPLES_META_UPDATE_TASK_ID = 'update_meta_for_couples'


class NodeInfoUpdater:

    STORAGE_STATE_CACHE_KEY = 'mastermind_storage'
    STORAGE_STATE_VERSION = '1'
    DEFAULT_STORAGE_STATE_VALID_TIME = 600

    def __init__(self, logging, node):
        logging.info("Created NodeInfoUpdater")
        self.__logging = logging
        self.__node = node
        self.__tq = timed_queue.TimedQueue()
        self.__tq.start()
        self.__session = elliptics.Session(self.__node)
        self.__session.set_timeout(config.get('wait_timeout', 5))
        self.__nodeUpdateTimestamps = (time.time(), time.time())
        self.__cache = Service('cache')

        self.loadNodes(delayed=False)

    def execute_tasks(self, delayed):
        try:

            stat = getattr(self.__session, 'stat_log_count',
                           self.__session.stat_log)
            storage.update_statistics(stat())

            if delayed:
                self.__tq.add_task_in(
                        GROUPS_META_UPDATE_TASK_ID,
                        config.get('symm_group_read_gap', 1),
                        self.update_symm_groups_async)
                self.__tq.add_task_in(
                        COUPLES_META_UPDATE_TASK_ID,
                        config.get('couple_read_gap', 1),
                        self.update_couples_meta_async)
            else:
                self.update_symm_groups_async()
                self.update_couples_meta_async()

        except Exception as e:
            self.__logging.info('Failed to initialize node updater: %s\n%s' % (str(e), traceback.format_exc()))
            pass

    def loadNodes(self, delayed=True):
        self.__logging.info('Start loading units')
        try:

            self.execute_tasks(delayed)

            try:
                max_group = int(EllAsyncResult(
                    self.__node.meta_session.read_data(keys.MASTERMIND_MAX_GROUP_KEY),
                    EllReadResult).get()[0].data)
            except:
                max_group = 0
            curr_max_group = max((g.group_id for g in storage.groups))
            if curr_max_group > max_group:
                EllAsyncResult(self.__node.meta_session.write_data(
                    keys.MASTERMIND_MAX_GROUP_KEY, str(curr_max_group)),
                    EllLookupResult).get()

        except Exception as e:
            self.__logging.error("Error while loading node stats: %s\n%s" % (str(e), traceback.format_exc()))
        finally:
            reload_period = config.get('nodes_reload_period', 60)
            self.__tq.add_task_in("load_nodes", reload_period, self.loadNodes)
            self.__nodeUpdateTimestamps = self.__nodeUpdateTimestamps[1:] + (time.time(),)
            bla.setConfigValue("dynamic_too_old_age", max(time.time() - self.__nodeUpdateTimestamps[0], reload_period * 3))

    def force_nodes_update(self, request):
        self.__logging.info('Forcing nodes update')
        try:
            self.__tq.add_task_in('load_nodes', 0, self.loadNodes)
            self.__logging.info('Task for nodes update was created successfully')
        except Exception:
            self.__logging.info('Task for nodes update has already been created')
            self.__tq.hurry('load_nodes')
        return True

    def update_symm_groups_async(self):

        _queue = set()
        def _process_group_metadata(meta, group):

            group.parse_meta(meta)
            couples = group.meta['couple']
            self.__logging.info('Read symmetric groups from group '
                '{0}: {1}'.format(group.group_id, couples))
            for group_id2 in couples:
                if group_id2 != group.group_id:
                    self.__logging.info('Scheduling update '
                        'for group {0}'.format(group_id2))
                    _queue.add(group_id2)

            couple_str = ':'.join((str(g) for g in sorted(couples)))

            self.__logging.debug('{0} in storage.couples: {1}'.format(
                couple_str, couple_str in storage.couples))
            # self.__logging.debug('Keys in storage.couples: {0}'.format(
            #     [str(c) for c in storage.couples]))

            if not couple_str in storage.couples:
                self.__logging.info('Creating couple {0}'.format(couple_str))
                for gid in couples:
                    if not gid in storage.groups:
                        self.__logging.info("Group {0} doesn't exist in "
                            "all_groups, add fake data with couples={1}".format(gid, couples))
                        storage.groups.add(gid)
                c = storage.couples.add([storage.groups[gid] for gid in couples])
                self.__logging.info('Created couple {0} {1}'.format(c, repr(c)))
            return

        try:
            requests = []

            def _session_setup(session, groups):
                session.add_groups(groups)
                return session

            groups = storage.groups.keys()

            for group in groups:
                group_id = group.group_id
                requests.append((functools.partial(_session_setup, self.__session, [group_id]),
                                 group_id, keys.SYMMETRIC_GROUPS_KEY))
            results = self._parallel_read(requests)
            [group.parse_meta(None) for group in groups if not group.group_id in results]

            while results:
                if _queue:
                    group_id = _queue.pop()
                    if group_id not in results:
                        continue
                    result = results.pop(group_id)
                else:
                    group_id, result = results.popitem()

                group = storage.groups[group_id]

                try:
                    self.__logging.debug('Reading symmetric groups '
                        'from group {0}'.format(group.group_id))
                    self._process_elliptics_response(_process_group_metadata,
                                                     result, group)
                except ValueError as e:
                    self.__logging.warn('Failed to read symmetric_groups '
                        'from group {0}: {1}'.format(group_id, e))
                    group.parse_meta(None)
                except Exception as e:
                    self.__logging.error('Failed to read symmetric_groups '
                        'from group {0}: {1}, {2}'.format(
                            group_id, e, traceback.format_exc()))
                    group.parse_meta(None)
                finally:
                    group.update_status_recursive()

        except Exception as e:
            self.__logging.error('Critical error during symmetric group '
                                 'update, {0}: {1}'.format(str(e),
                                     traceback.format_exc()))

    def update_couples_meta_async(self):
        try:
            requests = []

            couples = storage.couples.keys()

            for couple in couples:
                requests.append((self.__node.meta_session, couple,
                                 keys.MASTERMIND_COUPLE_META_KEY % str(couple)))
            results = self._parallel_read(requests)
            [couple.parse_meta(None) for couple in couples if not couple in results]

            while results:
                couple, result = results.popitem()
                try:
                    self.__logging.debug('Reading couple {0} metadata'.format(
                        str(couple)))
                    self._process_elliptics_response(couple.parse_meta, result)
                    self.__logging.info('Updated couple metadata (frozen) '
                        'for couple {0}'.format(str(couple)))
                except ValueError as e:
                    self.__logging.debug('Failed to read couple {0} metadata: '
                        '{1}'.format(couple, e))
                    couple.parse_meta(None)
                except Exception as e:
                    self.__logging.error('Failed to read couple {0} metadata: '
                        '{1}, {2}'.format(couple, e, traceback.format_exc()))
                    couple.parse_meta(None)
                finally:
                    couple.update_status()

        except Exception as e:
            self.__logging.error('Critical error during couples metadata '
                                 'update, {0}: {1}'.format(str(e),
                                     traceback.format_exc()))

    def _parallel_read(self, requests):
        results = {}
        for session, result_key, key in requests:
            if callable(session):
                session = session()

            # read_latest_async is for elliptics 2.24.14.15, in later versions
            # read_data returns AsyncResult object
            self.__logging.debug('Request to read {0} for groups {1}'.format(
                                 key.replace('\0', '\\0'), result_key))
            read = getattr(session, 'read_latest_async', session.read_data)

            try:
                results[result_key] = read(elliptics.Id(key))
            except Exception as e:
                self.__logging.error('Failed to read {0} for groups '
                    '{1}: {2}, {3}'.format(key, result_key,
                                           str(e), traceback.format_exc()))
                pass

        return results

    @staticmethod
    def _process_elliptics_response(processor, result, *args):
        # get result if it is wrapped in AsyncResult
        # TODO: change check to isinstance(result, elliptics.AsyncResult) when fixed
        if getattr(result, 'get'):
            try:
                result.wait()
            except Exception as e:
                raise ValueError(e)
            if not result.successful():
                raise ValueError('key not found')
            if not len(result.get()):
                raise ValueError('empty response')
            entry = result.get()[0]
            if getattr(entry, 'error', None) and entry.error.code:
                raise ValueError('result code {0}, msg {1}'.format(
                    entry.error.code, entry.error.message))
            result = entry.data

        return processor(result, *args)

    def stop(self):
        self.__tq.shutdown()
