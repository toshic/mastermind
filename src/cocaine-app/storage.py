# -*- coding: utf-8 -*-

import time
import socket
import traceback
import msgpack

import inventory

def ts_str(ts):
    return time.asctime(time.localtime(ts))

class Status(object):
    INIT = 'INIT'
    OK = 'OK'
    COUPLED = 'COUPLED'
    BAD = 'BAD'
    RO = 'RO'
    STALLED = 'STALLED'

class Repositary(object):
    def __init__(self, constructor):
        self.elements = {}
        self.constructor = constructor

    def add(self, *args, **kwargs):
        e = self.constructor(*args, **kwargs)
        self.elements[e] = e
        return e

    def get(self, key):
        return self.elements[key]

    def remove(self, key):
        return self.elements.pop(key)

    def __getitem__(self, key):
        return self.get(key)

    def __contains__(self, key):
        return key in self.elements

    def __iter__(self):
        return self.elements.__iter__()

    def __repr__(self):
        return '<Repositary object: [%s] >' % (', '.join((repr(e) for e in self.elements.itervalues())))


class NodeStat(object):
    def __init__(self, raw_stat=None, prev=None):

        if raw_stat:
            self.init(raw_stat, prev)
        else:
            self.free_space = 0.0
            self.rel_space = 0.0
            self.load_average = 0.0

            self.read_rps = 0
            self.write_rps = 0
            self.max_read_rps = 0
            self.max_write_rps = 0

    def init(self, raw_stat, prev=None):
        self.ts = time.time()

        self.last_read = raw_stat["storage_commands"]["READ"][0] + raw_stat["proxy_commands"]["READ"][0]
        self.last_write = raw_stat["storage_commands"]["WRITE"][0] + raw_stat["proxy_commands"]["WRITE"][0]

        self.total_space = float(raw_stat['counters']['DNET_CNTR_BLOCKS'][0]) * raw_stat['counters']['DNET_CNTR_BSIZE'][0]
        self.free_space = float(raw_stat['counters']['DNET_CNTR_BAVAIL'][0]) * raw_stat['counters']['DNET_CNTR_BSIZE'][0]
        self.rel_space = float(raw_stat['counters']['DNET_CNTR_BAVAIL'][0]) / raw_stat['counters']['DNET_CNTR_BLOCKS'][0]
        self.load_average = float((raw_stat['counters'].get('DNET_CNTR_DU1') or raw_stat['counters']["DNET_CNTR_LA1"])[0]) / 100

        if prev:
            dt = self.ts - prev.ts

            self.read_rps = (self.last_read - prev.last_read)/dt
            self.write_rps = (self.last_write - prev.last_write)/dt

            # Disk usage should be used here instead of load average
            self.max_read_rps = max(self.read_rps / self.load_average, 100)

            self.max_write_rps = max(self.write_rps / self.load_average, 100)

        else:
            self.read_rps = 0
            self.write_rps = 0

            # Tupical SATA HDD performance is 100 IOPS
            # It will be used as first estimation for maximum node performance
            self.max_read_rps = 100
            self.max_write_rps = 100

    def __add__(self, other):
        res = NodeStat()

        res.ts = min(self.ts, other.ts)

        res.total_space = self.total_space + other.total_space
        res.free_space = self.free_space + other.free_space
        res.rel_space = min(self.rel_space, other.rel_space)
        res.load_average = max(self.load_average, other.load_average)

        res.read_rps = self.read_rps + other.read_rps
        res.write_rps = self.write_rps + other.write_rps

        res.max_read_rps = self.max_read_rps + other.max_read_rps
        res.max_write_rps = self.max_write_rps + other.max_write_rps

        return res

    def __mul__(self, other):
        res = NodeStat()
        res.ts = min(self.ts, other.ts)

        res.total_space = min(self.total_space, other.total_space)
        res.free_space = min(self.free_space, other.free_space)
        res.rel_space = min(self.rel_space, other.rel_space)
        res.load_average = max(self.load_average, other.load_average)

        res.read_rps = max(self.read_rps, other.read_rps)
        res.write_rps = max(self.write_rps, other.write_rps)

        res.max_read_rps = min(self.max_read_rps, other.max_read_rps)
        res.max_write_rps = min(self.max_write_rps, other.max_write_rps)

        return res

    def __repr__(self):
        return '<NodeStat object: ts=%s, write_rps=%d, max_write_rps=%d, read_rps=%d, max_read_rps=%d, total_space=%d, free_space=%d>' % (ts_str(self.ts), self.write_rps, self.max_write_rps, self.read_rps, self.max_read_rps, self.total_space, self.free_space)


class Host(object):
    def __init__(self, addr):
        self.addr = addr
        self.nodes = []

    def hostname(self):
        return socket.gethostbyaddr(self.addr)[0]


    def index(self):
        return self.__str__()

    def get_dc(self):
        return inventory.get_dc_by_host(self.addr)

    def __eq__(self, other):
        if isinstance(other, str):
            return self.addr == other

        if isinstance(other, Host):
            return self.addr == other.addr

        return False

    def __hash__(self):
        return hash(self.__str__())

    def __repr__(self):
        return '<Host object: addr=%s, nodes=[%s] >' % (self.addr, ', '.join((repr(n) for n in self.nodes)))

    def __str__(self):
        return self.addr

class Node(object):
    def __init__(self, host, port, group):
        self.host = host
        self.port = int(port)
        self.group = group
        self.host.nodes.append(self)
        self.group.add_node(self)

        self.stat = None

        self.destroyed = False
        self.read_only = False
        self.status = Status.INIT
        self.status_text = "Node %s is not inititalized yet" % (self.__str__())


    def get_host(self):
        return self.host

    def destroy(self):
        self.destroyed = True
        self.host.nodes.remove(self)
        self.host = None
        self.group.remove_node(self)

    def update_statistics(self, new_stat):
        stat = NodeStat(new_stat, self.stat)
        self.stat = stat

    def update_status(self):
        if self.destroyed:
            self.status = Status.BAD
            self.status_text = "Node %s is destroyed" % (self.__str__())

        elif not self.stat:
            self.status = Status.INIT
            self.status_text = "No statistics gathered for node %s" % (self.__str__())

        elif self.stat.ts < (time.time() - 120):
            self.status = Status.STALLED
            self.status_text = "Statistics for node %s is too old: it was gathered %d seconds ago" % (self.__str__(), int(time.time() - self.stat.ts))

        elif self.read_only:
            self.status = Status.RO
            self.status_text = "Node %s is in Read-Only state" % (self.__str__())

        else:
            self.status = Status.OK
            self.status_text = "Node %s is OK" % (self.__str__())

        #if self.group.group_id == 1:
        #    print 'Update status: ', repr(self)

        return self.status

    def info(self):
        res = {}

        res['addr'] = self.__str__()
        res['status'] = self.status
        #res['stat'] = str(self.stat)

        return res

    def __repr__(self):
        if self.destroyed:
            return '<Node object: DESTROYED!>'

        return '<Node object: host=%s, port=%d, group=%s, status=%s, read_only=%s, stat=%s>' % (str(self.host), self.port, str(self.group), self.status, str(self.read_only), repr(self.stat))

    def __str__(self):
        if self.destroyed:
            raise Exception('Node object is destroyed')

        return '%s:%d' % (self.host.addr, self.port)

    def __hash__(self):
        return hash(self.__str__())

    def __eq__(self, other):
        if isinstance(other, str):
            return self.__str__() == other

        if isinstance(other, Node):
            return self.addr == other.addr and self.port == other.port


class Group(object):

    DEFAULT_NAMESPACE = 'default'

    def __init__(self, group_id, nodes=None):
        self.group_id = group_id
        self.status = Status.INIT
        self.nodes = []
        self.couple = None
        self.meta = None
        self.status_text = "Group %s is not inititalized yet" % (self.__str__())

        if nodes:
            for node in nodes:
                self.add_node(node)

    def add_node(self, node):
        self.nodes.append(node)

    def has_node(self, node):
        return node in self.nodes

    def remove_node(self, node):
        self.nodes.remove(node)

    def parse_meta(self, meta):
        if meta is None:
            self.meta = None
            self.status = Status.BAD
            return

        parsed = msgpack.unpackb(meta)
        if isinstance(parsed, tuple):
            self.meta = {'version': 1, 'couple': parsed, 'namespace': self.DEFAULT_NAMESPACE}
        elif isinstance(parsed, dict) and parsed['version'] == 2:
            self.meta = parsed
        else:
            raise Exception('Unable to parse meta')

    def get_stat(self):
        return reduce(lambda res, x: res + x, [node.stat for node in self.nodes])

    def update_status(self):
        if not self.nodes:
            self.status = Status.INIT
            self.status_text = "Group %s is in INIT state because there is no nodes serving this group" % (self.__str__())

        logging.info('In group %d meta = %s' % (self.group_id, str(self.meta)))
        if (not self.meta) or (not 'couple' in self.meta) or (not self.meta['couple']):
            self.status = Status.INIT
            self.status_text = "Group %s is in INIT state because there is no coupling info" % (self.__str__())
            return self.status

        statuses = tuple(node.update_status() for node in self.nodes)

        if Status.RO in statuses:
            self.status = Status.RO
            self.status_text = "Group %s is in Read-Only state because there is RO nodes" % (self.__str__())
            return self.status

        if not all([st == Status.OK for st in statuses]):
            self.status = Status.BAD
            self.status_text = "Group %s is in Bad state because some node statuses are not OK" % (self.__str__())
            return self.status

        if (not self.couple) and self.meta['couple']:
            self.status = Status.BAD
            self.status_text = "Group %s is in Bad state because couple did not created" % (self.__str__())
            return self.status

        elif not self.couple.check_groups(self.meta['couple']):
            self.status = Status.BAD
            self.status_text = "Group %s is in Bad state because couple check fails" % (self.__str__())
            return self.status

        elif not self.meta['namespace']:
            self.status = Status.BAD
            self.status_text = "Group %s is in Bad state because no namespace has been assigned to it" % (self.__str__())
            return self.status

        elif self.meta['namespace'] != self.couple.namespace:
            self.status = Status.BAD
            self.status_text = "Group %s is in Bad state because its namespace doesn't correspond to couple namespace (%s)" % (self.__str__(), self.couple.namespace)
            return self.status

        self.status = Status.COUPLED
        self.status_text = "Group %s is OK" % (self.__str__())

        return self.status

    def info(self):
        res = {}

        res['status'] = self.status
        res['status_text'] = self.status_text
        res['nodes'] = [n.info() for n in self.nodes]
        if self.couple:
            res['couples'] = self.couple.as_tuple()
        else:
            res['couples'] = None
        if self.meta:
            res['namespace'] = self.meta['namespace']

        return res

    def __hash__(self):
        return hash(self.group_id)

    def __str__(self):
        return '%d' % (self.group_id)

    def __repr__(self):
        return '<Group object: group_id=%d, status=%s nodes=[%s], meta=%s, couple=%s>' % (self.group_id, self.status, ', '.join((repr(n) for n in self.nodes)), str(self.meta), str(self.couple))

    def __eq__(self, other):
        return self.group_id == other

class Couple(object):
    def __init__(self, groups):
        self.status = Status.INIT
        self.groups = sorted(groups, key=lambda group: group.group_id)
        for group in self.groups:
            if group.couple:
                raise Exception('Group %s is already in couple' % (repr(group)))

            group.couple = self

    def get_stat(self):
        try:
            return reduce(lambda res, x: res * x, [group.get_stat() for group in self.groups])
        except TypeError:
            return None

    def update_status(self):
        statuses = [group.update_status() for group in self.groups]

        meta = self.groups[0].meta
        if any([meta != group.meta for group in self.groups]):
            self.status = Status.BAD
            return self.status

        if all([st == Status.COUPLED for st in statuses]):
            self.status = Status.OK
            return self.status

        if Status.INIT in statuses:
            self.status = Status.INIT

        elif Status.BAD in statuses:
            self.status = Status.BAD

        elif Status.RO in statuses:
            self.status = Status.RO

        else:
            self.status = Status.BAD

        return self.status

    def check_groups(self, groups):

        for group in self.groups:
            if group.meta is None or not 'couple' in group.meta or not group.meta['couple']:
                return False

            if set(groups) != set(group.meta['couple']):
                return False

        if set(groups) != set((g.group_id for g in self.groups)):
            self.status = Status.BAD
            return False

        return True

    def destroy(self):
        for group in self.groups:
            group.couple = None
            group.meta = None

        couples.remove(self)
        self.groups = []
        self.status = Status.INIT

    def compose_meta(self, namespace):
        return {
            'version': 2,
            'couple': self.as_tuple(),
            'namespace': namespace,
        }

    @property
    def namespace(self):
        assert self.groups
        return self.groups[0].meta['namespace']

    def as_tuple(self):
        return tuple(group.group_id for group in self.groups)

    def __contains__(self, group):
        return group in self.groups

    def __iter__(self):
        return self.groups.__iter__()

    def __len__(self):
        return len(self.groups)

    def __str__(self):
        return ':'.join([str(group) for group in self.groups])

    def __hash__(self):
        return hash(self.__str__())

    def __eq__(self, other):
        if isinstance(other, str):
            return self.__str__() == other

        if isinstance(other, Couple):
            return self.groups == other.groups

    def __repr__(self):
        return '<Couple object: status=%s, groups=[%s] >' % (self.status, ', '.join([repr(g) for g in self.groups]))



hosts = Repositary(Host)
groups = Repositary(Group)
nodes = Repositary(Node)
couples = Repositary(Couple)

from cocaine.logging import Logger
logging = Logger()
def update_statistics(stats):
    for stat in stats:
        logging.info("Stats: %s %s" % (str(stat['group_id']), stat['addr']))
        try:
            if not stat['addr'] in nodes:
                addr = stat['addr'].split(':')
                if not addr[0] in hosts:
                    host = hosts.add(addr[0])
                    logging.debug('Adding host %s' % (addr[0]))
                else:
                    host = hosts[addr[0]]

                if not stat['group_id'] in groups:
                    group = groups.add(stat['group_id'])
                    logging.debug('Adding group %d' % stat['group_id'])
                else:
                    group = groups[stat['group_id']]

                n = nodes.add(host, addr[1], group)
                logging.debug('Adding node %d -> %s:%s' % (stat['group_id'], addr[0], addr[1]))

            node = nodes[stat['addr']]
            if node.group.group_id != stat['group_id']:
                raise Exception('Node group_id = %d, group_id from stat: %d' % (node.group.group_id, stat['group_id']))

            logging.info('Updating statistics for node %s' % (str(node)))
            node.update_statistics(stat)
            logging.info('Updating status for group %d' % (stat['group_id']))
            groups[stat['group_id']].update_status()

        except Exception as e:
            logging.error('Unable to process statictics for node %s group_id %d: %s' % (stat['addr'], stat['group_id'], traceback.format_exc()))

'''
h = hosts.add('95.108.228.31')
g = groups.add(1)
n = nodes.add(hosts['95.108.228.31'], 1025, groups[1])

g2 = groups.add(2)
nodes.add(h, 1026, g2)

couple = couples.add([g, g2])

print '1:2' in couples
groups[1].parse_meta({'couple': (1,2)})
groups[2].parse_meta({'couple': (1,2)})
couple.update_status()

print repr(couples)
print 1 in groups
print [g for g in couple]
couple.destroy()
print repr(couples)
'''
