# encoding: utf-8
import copy
import sys
import traceback

from cocaine.worker import Worker
from cocaine.logging import Logger
import elliptics
import msgpack

import balancelogicadapter as bla
import balancelogic
import storage


logging = Logger()

logging.info("balancer.py")

SYMMETRIC_GROUPS_KEY = "metabalancer\0symmetric_groups"


class Balancer(object):
    def __init__(self, n):
        self.node = n

    def get_groups(self, request):
        return tuple(group.group_id for group in storage.groups)

    def get_symmetric_groups(self, request):
        result = [couple.as_tuple() for couple in storage.couples if couple.status == storage.Status.OK]
        logging.debug("good_symm_groups: " + str(result))
        return result

    def get_bad_groups(self, request):
        result = [couple.as_tuple() for couple in storage.couples if couple.status != storage.Status.OK]
        logging.debug("bad_symm_groups: " + str(result))
        return result

    def get_empty_groups(self, request):
        logging.info("len(storage.groups) = %d" % (len(storage.groups.elements)))
        logging.info("groups: %s" % str([(group.group_id, group.couple) for group in storage.groups if group.couple is None]))
        result = [group.group_id for group in storage.groups if group.couple is None]
        logging.debug("uncoupled groups: " + str(result))
        return result

    def get_group_weights(self, request):
        try:
            sizes = set()
            namespaces = set()
            all_symm_group_objects = []
            for couple in storage.couples:
                if couple.status != storage.Status.OK:
                    continue

                symm_group = bla.SymmGroup(couple)
                sizes.add(len(couple))
                namespaces.add(couple.namespace)
                all_symm_group_objects.append(symm_group)
                logging.debug(str(symm_group))

            result = {}

            for namespace in namespaces:
                for size in sizes:
                    (group_weights, info) = balancelogic.rawBalance(all_symm_group_objects,
                                                                    bla.getConfig(),
                                                                    bla._and(bla.GroupSizeEquals(size),
                                                                             bla.GroupNamespaceEquals(namespace)))
                    result.setdefault(namespace, {})[size] = [item for item in group_weights.items()]
                    logging.info("Cluster info: " + str(info))

            logging.info(str(result))
            return result
        except Exception as e:
            logging.error("Balancelogic error: " + str(e) + "\n" + traceback.format_exc())
            return {'Balancelogic error': str(e)}

    def balance(self, request):
        try:
            logging.info("----------------------------------------")
            logging.info("New request" + str(len(request)))
            logging.info(request)

            weighted_groups = self.get_group_weights(self.node)

            target_groups = []

            if manifest.get("symmetric_groups", False):
                lsymm_groups = weighted_groups[request[0]]

                for (gr_list, weight) in lsymm_groups:
                    logging.info("gr_list: %s %d" % (str(gr_list), request[0]))
                    grl = {'rating': weight, 'groups': gr_list}
                    logging.info("grl: %s" % str(grl))
                    target_groups.append(grl)

                logging.info("target_groups: %s" % str(target_groups))
                if target_groups:
                    sorted_groups = sorted(target_groups, key=lambda gr: gr['rating'], reverse=True)[0]
                    logging.info(sorted_groups)
                    result = (sorted_groups['groups'], request[1])
                else:
                    result = ([], request[1])

            else:
                for group_id in bla.all_group_ids():
                    target_groups.append(bla.get_group(int(group_id)))
                sorted_groups = sorted(target_groups, key=lambda gr: gr.freeSpaceInKb(), reverse=True)[:int(request[0])]
                logging.info(sorted_groups)
                result = ([g.groupId() for g in sorted_groups], request[1])

            logging.info("result: %s" % str(result))
            return result
        except Exception as e:
            logging.error("Balancer error: " + str(e) + "\n" + traceback.format_exc())
            return {'Balancer error': str(e)}



    def repair_groups(self, request):
        try:
            logging.info("----------------------------------------")
            logging.info("New repair groups request: " + str(request))
            logging.info(request)

            group_id = int(request)

            if not group_id in storage.groups:
                return {'Balancer error': 'Group %d is not found' % (group)}

            group = storage.groups[group_id]

            bad_couples = []
            for couple in storage.couples:
                if group in couple:
                    if couple.status == storage.Status.OK:
                        logging.error("Balancer error: cannot repair, group %d is in couple %s" % (group_id, str(couple)))
                        return {"Balancer error" : "cannot repair, group %d is in couple %s" % (group_id, str(couple))}
                    bad_couples.append(couple)

            if not bad_couples:
                logging.error("Balancer error: cannot repair, group %d is not a member of any couple" % group_id)
                return {"Balancer error" : "cannot repair, group %d is not a member of any couple" % group_id}

            if len(bad_couples) > 1:
                logging.error("Balancer error: cannot repair, group %d is a member of several couples: %s" % (group_id, str(bad_couples)))
                return {"Balancer error" : "cannot repair, group %d is a member of several couples: %s" % (group_id, str(bad_couples))}

            couple = bad_couples[0]

            # checking namespaces in all couple groups
            for g in couple:
                if g.group_id == group_id:
                    continue
                if g.meta is None:
                    logging.error('Balancer error: group %d (coupled with group %d) has no metadata' % (g.group_id, group_id))
                    return {'Balancer error': 'group %d (coupled with group %d) has no metadata' % (g.group_id, group_id)}

            namespaces = [g.meta['namespace'] for g in couple if g.group_id != group_id]
            if not all(ns == namespaces[0] for ns in namespaces):
                logging.error('Balancer error: namespaces of groups coupled with group %d are not the same: %s' % (group_id, namespaces))
                return {'Balancer error': 'namespaces of groups coupled with group %d are not the same: %s' % (group_id, namespaces)}

            (good, bad) = make_symm_group(self.node, couple, namespaces[0])
            if bad:
                raise bad[1]

            return {"message": "Successfully repaired couple", 'couple': str(couple)}

        except Exception as e:
            logging.error("Balancer error: " + str(e) + "\n" + traceback.format_exc())
            return {'Balancer error': str(e)}

    def get_group_info(self, request):
        try:
            group = int(request)
            logging.info('get_group_info: request: %s, %s' % (str(request), repr(storage.groups[group])))

            if not group in storage.groups:
                return {'Balancer error': 'Group %d is not found' % (group)}

            return storage.groups[group].info()

        except Exception as e:
            logging.error("Balancer error: " + str(e) + "\n" + traceback.format_exc())
            return {'Balancer error': str(e)}

    def couple_groups(self, request):
        try:
            logging.info("----------------------------------------")
            logging.info("New couple groups request: " + str(request))
            logging.info(request)
            uncoupled_groups = self.get_empty_groups(self.node)
            dc_by_group_id = {}
            group_by_dc = {}
            for group_id in uncoupled_groups:
                group = storage.groups[group_id]
                dc = group.nodes[0].host.get_dc()
                dc_by_group_id[group_id] = dc
                groups_in_dc = group_by_dc.setdefault(dc, [])
                groups_in_dc.append(group_id)
            logging.info("dc by group: %s" % str(dc_by_group_id))
            logging.info("group_by_dc: %s" % str(group_by_dc))
            size = int(request[0])
            mandatory_groups = [int(g) for g in request[1]]

            # check mandatory set
            for group_id in mandatory_groups:
                if group_id not in uncoupled_groups:
                    raise Exception("group %d is coupled" % group_id)
                dc = dc_by_group_id[group_id]
                if dc not in group_by_dc:
                    raise Exception("groups must be in different dcs")
                del group_by_dc[dc]

            groups_to_couple = copy.copy(mandatory_groups)

            # need better algorithm for this. For a while - only 1 try and random selection
            n_groups_to_add = size - len(groups_to_couple)
            if n_groups_to_add > len(group_by_dc):
                raise Exception("Not enough dcs")
            if n_groups_to_add < 0:
                raise Exception("Too many mandatory groups")

            # why not use random.sample
            some_dcs = group_by_dc.keys()[:n_groups_to_add]

            for dc in some_dcs:
                groups_to_couple.append(group_by_dc[dc].pop())

            try:
                namespace = request[2]
            except IndexError:
                namespace = storage.Group.DEFAULT_NAMESPACE

            couple = storage.couples.add([storage.groups[g] for g in groups_to_couple])
            (good, bad) = make_symm_group(self.node, couple, namespace)
            if bad:
                raise bad[1]

            return groups_to_couple
        except Exception as e:
            logging.error("Balancer error: " + str(e) + "\n" + traceback.format_exc())
            return {'Balancer error': str(e)}


    def break_couple(self, request):
        try:
            logging.info("----------------------------------------")
            logging.info("New break couple request: " + str(request))
            logging.info(request)

            couple_str = ':'.join(map(str, sorted(request[0], key=lambda x: int(x))))
            if not couple_str in storage.couples:
                raise KeyError('Couple %s was not found' % (couple_str))

            couple = storage.couples[couple_str]
            confirm = request[1]

            logging.info("groups: %s; confirmation: \"%s\"" % (couple_str, confirm))

            correct_confirm = "Yes, I want to break "
            if couple.status == storage.Status.OK:
                correct_confirm += "good"
            else:
                correct_confirm += "bad"

            correct_confirm += " couple " + couple_str

            if confirm != correct_confirm:
                raise Exception('Incorrect confirmation string')

            kill_symm_group(self.node, [group.group_id for group in couple])
            couple.destroy()

            return True

        except Exception as e:
            logging.error("Balancer error: " + str(e) + "\n" + traceback.format_exc())
            return {'Balancer error': str(e)}

    def get_next_group_number(self, request):
        try:
            groups_count = int(request)
            if groups_count < 0 or groups_count > 100:
                raise Exception('Incorrect groups count')

            try:
                max_group = int(self.node.meta_session.read_data("mastermind:max_group"))
            except:
                max_group = 0

            new_max_group = max_group + groups_count
            self.node.meta_session.write_data("mastermind:max_group", str(new_max_group))

            return range(max_group+1, max_group+1 + groups_count)

        except Exception as e:
            logging.error("Mastermind error: " + str(e) + "\n" + traceback.format_exc())
            return {'Mastermind error': str(e)}


def handlers(b):
    handlers = []
    for attr_name in dir(b):
        attr = b.__getattribute__(attr_name)
        if not callable(attr) or attr_name.startswith('__'):
            continue
        handlers.append(attr)
    return handlers

def kill_symm_group(n, groups):
    logging.info("Killing symm groups: %s" % str(groups))
    s = elliptics.Session(n)
    s.add_groups(groups)
    s.remove(SYMMETRIC_GROUPS_KEY)


def make_symm_group(n, couple, namespace):
    logging.info("writing couple info: " + str(couple))
    logging.info('groups in couple %s are being assigned namespace "%s"' % (couple, namespace))

    s = elliptics.Session(n)
    good = []
    bad = ()
    for group in couple:
        try:
            packed = msgpack.packb(couple.compose_meta(namespace))
            logging.info("packed couple for group %d: \"%s\"" % (group.group_id, str(packed).encode("hex")))
            s.add_groups([group.group_id])
            s.write_data(SYMMETRIC_GROUPS_KEY, packed)
            good.append(group.group_id)
        except Exception as e:
            logging.error("Failed to write symm group info, group %d: %s\n%s"
                          % (group.group_id, str(e), traceback.format_exc()))
            bad = (group.group_id, e)
            break
    return (good, bad)

