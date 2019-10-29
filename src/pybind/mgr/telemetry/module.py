"""
Telemetry module for ceph-mgr

Collect statistics from Ceph cluster and send this back to the Ceph project
when user has opted-in
"""
import errno
import hashlib
import json
import rbd
import re
import requests
import uuid
import time
from datetime import datetime, timedelta
from threading import Event
from collections import defaultdict

from mgr_module import MgrModule


ALL_CHANNELS = ['basic', 'ident', 'crash', 'device']

LICENSE='sharing-1-0'
LICENSE_NAME='Community Data License Agreement - Sharing - Version 1.0'
LICENSE_URL='https://cdla.io/sharing-1-0/'

# If the telemetry revision has changed since this point, re-require
# an opt-in.  This should happen each time we add new information to
# the telemetry report.
LAST_REVISION_RE_OPT_IN = 2

# Latest revision of the telemetry report.  Bump this each time we make
# *any* change.
REVISION = 3

# History of revisions
# --------------------
#
# Version 1:
#   Mimic and/or nautilus are lumped together here, since
#   we didn't track revisions yet.
#
# Version 2:
#   - added revision tracking, nagging, etc.
#   - added config option changes
#   - added channels
#   - added explicit license acknowledgement to the opt-in process
#
# Version 3:
#   - added device health metrics (i.e., SMART data, minus serial number)
#   - added CephFS metadata (how many MDSs, fs features, how many data pools)
#   - remove crush_rule
#   - added more pool metadata (rep vs ec, cache tiering mode, ec profile)
#   - added host count, and counts for hosts with each of (mon, osd, mds, mgr)
#   - whether an OSD cluster network is in use
#   - rbd pool and image count, and rbd mirror mode (pool-level)

class Module(MgrModule):
    config = dict()

    metadata_keys = [
            "arch",
            "ceph_version",
            "os",
            "cpu",
            "kernel_description",
            "kernel_version",
            "distro_description",
            "distro"
    ]

    MODULE_OPTIONS = [
        {
            'name': 'url',
            'type': 'str',
            'default': 'https://telemetry.ceph.com/report'
        },
        {
            'name': 'enabled',
            'type': 'bool',
            'default': False
        },
        {
            'name': 'last_opt_revision',
            'type': 'int',
            'default': 1,
        },
        {
            'name': 'leaderboard',
            'type': 'bool',
            'default': False
        },
        {
            'name': 'description',
            'type': 'str',
            'default': None
        },
        {
            'name': 'contact',
            'type': 'str',
            'default': None
        },
        {
            'name': 'organization',
            'type': 'str',
            'default': None
        },
        {
            'name': 'proxy',
            'type': 'str',
            'default': None
        },
        {
            'name': 'interval',
            'type': 'int',
            'default': 24,
            'min': 8
        },
        {
            'name': 'channel_basic',
            'type': 'bool',
            'default': True,
            'desc': 'Share basic cluster information (size, version)',
        },
        {
            'name': 'channel_ident',
            'type': 'bool',
            'default': False,
            'description': 'Share a user-provided description and/or contact email for the cluster',
        },
        {
            'name': 'channel_crash',
            'type': 'bool',
            'default': True,
            'description': 'Share metadata about Ceph daemon crashes (version, stack straces, etc)',
        },
        {
            'name': 'channel_device',
            'type': 'bool',
            'default': True,
            'description': 'Share device health metrics (e.g., SMART data, minus potentially identifying info like serial numbers)',
        },
    ]

    COMMANDS = [
        {
            "cmd": "telemetry status",
            "desc": "Show current configuration",
            "perm": "r"
        },
        {
            "cmd": "telemetry send",
            "desc": "Force sending data to Ceph telemetry",
            "perm": "rw"
        },
        {
            "cmd": "telemetry show "
                   "name=channels,type=CephString,n=N,req=False",
            "desc": "Show last report or report to be sent",
            "perm": "r"
        },
        {
            "cmd": "telemetry on name=license,type=CephString,req=false",
            "desc": "Enable telemetry reports from this cluster",
            "perm": "rw",
        },
        {
            "cmd": "telemetry off",
            "desc": "Disable telemetry reports from this cluster",
            "perm": "rw",
        },
    ]

    @property
    def config_keys(self):
        return dict((o['name'], o.get('default', None)) for o in self.MODULE_OPTIONS)

    def __init__(self, *args, **kwargs):
        super(Module, self).__init__(*args, **kwargs)
        self.event = Event()
        self.run = False
        self.last_upload = None
        self.last_report = dict()
        self.report_id = None
        self.salt = None

    def config_notify(self):
        for opt in self.MODULE_OPTIONS:
            setattr(self,
                    opt['name'],
                    self.get_module_option(opt['name']))
            self.log.debug(' %s = %s', opt['name'], getattr(self, opt['name']))
        # wake up serve() thread
        self.event.set()

    def load(self):
        self.last_upload = self.get_store('last_upload', None)
        if self.last_upload is not None:
            self.last_upload = int(self.last_upload)

        self.report_id = self.get_store('report_id', None)
        if self.report_id is None:
            self.report_id = str(uuid.uuid4())
            self.set_store('report_id', self.report_id)

        self.salt = self.get_store('salt', None)
        if not self.salt:
            self.salt = str(uuid.uuid4())
            self.set_store('salt', self.salt)

    def gather_osd_metadata(self, osd_map):
        keys = ["osd_objectstore", "rotational"]
        keys += self.metadata_keys

        metadata = dict()
        for key in keys:
            metadata[key] = defaultdict(int)

        for osd in osd_map['osds']:
            for k, v in self.get_metadata('osd', str(osd['osd'])).items():
                if k not in keys:
                    continue

                metadata[k][v] += 1

        return metadata

    def gather_mon_metadata(self, mon_map):
        keys = list()
        keys += self.metadata_keys

        metadata = dict()
        for key in keys:
            metadata[key] = defaultdict(int)

        for mon in mon_map['mons']:
            for k, v in self.get_metadata('mon', mon['name']).items():
                if k not in keys:
                    continue

                metadata[k][v] += 1

        return metadata

    def gather_configs(self):
        # cluster config options
        cluster = set()
        r, outb, outs = self.mon_command({
            'prefix': 'config dump',
            'format': 'json'
        });
        if r != 0:
            return {}
        try:
            dump = json.loads(outb)
        except json.decoder.JSONDecodeError:
            return {}
        for opt in dump:
            name = opt.get('name')
            if name:
                cluster.add(name)
        # daemon-reported options (which may include ceph.conf)
        active = set()
        ls = self.get("modified_config_options");
        for opt in ls.get('options', {}):
            active.add(opt)
        return {
            'cluster_changed': sorted(list(cluster)),
            'active_changed': sorted(list(active)),
        }

    def gather_crashinfo(self):
        crashlist = list()
        errno, crashids, err = self.remote('crash', 'ls')
        if errno:
            return ''
        for crashid in crashids.split():
            cmd = {'id': crashid}
            errno, crashinfo, err = self.remote('crash', 'do_info', cmd, '')
            if errno:
                continue
            c = json.loads(crashinfo)
            del c['utsname_hostname']
            (etype, eid) = c.get('entity_name', '').split('.')
            m = hashlib.sha1()
            m.update(self.salt.encode('utf-8'))
            m.update(eid.encode('utf-8'))
            m.update(self.salt.encode('utf-8'))
            c['entity_name'] = etype + '.' + m.hexdigest()
            crashlist.append(c)
        return crashlist

    def get_active_channels(self):
        r = []
        if self.channel_basic:
            r.append('basic')
        if self.channel_crash:
            r.append('crash')
        if self.channel_device:
            r.append('device')
        return r

    def gather_device_report(self):
        try:
            time_format = self.remote('devicehealth', 'get_time_format')
        except:
            return None
        cutoff = datetime.utcnow() - timedelta(hours=self.interval * 2)
        min_sample = cutoff.strftime(time_format)

        devices = self.get('devices')['devices']

        res = {}
        for d in devices:
            devid = d['devid']
            try:
                m = self.remote('devicehealth', 'get_recent_device_metrics',
                                devid, min_sample)
            except:
                continue

            # anonymize host id
            try:
                host = d['location'][0]['host']
            except:
                continue
            anon_host = self.get_store('host-id/%s' % host)
            if not anon_host:
                anon_host = str(uuid.uuid1())
                self.set_store('host-id/%s' % host, anon_host)
            m['host_id'] = anon_host

            # anonymize device id
            (vendor, model, serial) = devid.split('_')
            anon_devid = self.get_store('devid-id/%s' % devid)
            if not anon_devid:
                anon_devid = '%s_%s_%s' % (vendor, model, uuid.uuid1())
                self.set_store('devid-id/%s' % devid, anon_devid)

            self.log.info('devid %s / %s, host %s / %s' % (devid, anon_devid,
                                                           host, anon_host))

            # anonymize the smartctl report itself
            for k in ['serial_number']:
                if k in m:
                    m.pop(k)

            res[anon_devid] = m
        return res

    def compile_report(self, channels=[]):
        if not channels:
            channels = self.get_active_channels()
        report = {
            'leaderboard': False,
            'report_version': 1,
            'report_timestamp': datetime.utcnow().isoformat(),
            'report_id': self.report_id,
            'channels': channels,
            'channels_available': ALL_CHANNELS,
            'license': LICENSE,
        }

        if 'ident' in channels:
            if self.leaderboard:
                report['leaderboard'] = True
            for option in ['description', 'contact', 'organization']:
                report[option] = getattr(self, option)

        if 'basic' in channels:
            mon_map = self.get('mon_map')
            osd_map = self.get('osd_map')
            service_map = self.get('service_map')
            fs_map = self.get('fs_map')
            df = self.get('df')

            report['created'] = mon_map['created']

            # mons
            v1_mons = 0
            v2_mons = 0
            ipv4_mons = 0
            ipv6_mons = 0
            for mon in mon_map['mons']:
                for a in mon['public_addrs']['addrvec']:
                    if a['type'] == 'v2':
                        v2_mons += 1
                    elif a['type'] == 'v1':
                        v1_mons += 1
                    if a['addr'].startswith('['):
                        ipv6_mons += 1
                    else:
                        ipv4_mons += 1
            report['mon'] = {
                'count': len(mon_map['mons']),
                'features': mon_map['features'],
                'min_mon_release': mon_map['min_mon_release'],
                'v1_addr_mons': v1_mons,
                'v2_addr_mons': v2_mons,
                'ipv4_addr_mons': ipv4_mons,
                'ipv6_addr_mons': ipv6_mons,
            }

            report['config'] = self.gather_configs()

            # pools
            report['rbd'] = {
                'num_pools': 0,
                'num_images_by_pool': [],
                'mirroring_by_pool': [],
            }
            num_pg = 0
            report['pools'] = list()
            for pool in osd_map['pools']:
                num_pg += pool['pg_num']
                ec_profile = {}
                if pool['erasure_code_profile']:
                    orig = osd_map['erasure_code_profiles'].get(
                        pool['erasure_code_profile'], {})
                    ec_profile = {
                        k: orig[k] for k in orig.keys()
                        if k in ['k', 'm', 'plugin', 'technique',
                                 'crush-failure-domain', 'l']
                    }
                report['pools'].append(
                    {
                        'pool': pool['pool'],
                        'type': pool['type'],
                        'pg_num': pool['pg_num'],
                        'pgp_num': pool['pg_placement_num'],
                        'size': pool['size'],
                        'min_size': pool['min_size'],
                        'pg_autoscale_mode': pool['pg_autoscale_mode'],
                        'target_max_bytes': pool['target_max_bytes'],
                        'target_max_objects': pool['target_max_objects'],
                        'type': ['', 'replicated', '', 'erasure'][pool['type']],
                        'erasure_code_profile': ec_profile,
                        'cache_mode': pool['cache_mode'],
                    }
                )
                if 'rbd' in pool['application_metadata']:
                    report['rbd']['num_pools'] += 1
                    ioctx = self.rados.open_ioctx(pool['pool_name'])
                    report['rbd']['num_images_by_pool'].append(
                        sum(1 for _ in rbd.RBD().list2(ioctx)))
                    report['rbd']['mirroring_by_pool'].append(
                        rbd.RBD().mirror_mode_get(ioctx) != rbd.RBD_MIRROR_MODE_DISABLED)

            # osds
            cluster_network = False
            for osd in osd_map['osds']:
                if osd['up'] and not cluster_network:
                    front_ip = osd['public_addrs']['addrvec'][0]['addr'].split(':')[0]
                    back_ip = osd['public_addrs']['addrvec'][0]['addr'].split(':')[0]
                    if front_ip != back_ip:
                        cluster_network = True
            report['osd'] = {
                'count': len(osd_map['osds']),
                'require_osd_release': osd_map['require_osd_release'],
                'require_min_compat_client': osd_map['require_min_compat_client'],
                'cluster_network': cluster_network,
            }

            # cephfs
            report['fs'] = {
                'count': len(fs_map['filesystems']),
                'feature_flags': fs_map['feature_flags'],
                'num_standby_mds': len(fs_map['standbys']),
                'filesystems': [],
            }
            num_mds = len(fs_map['standbys'])
            for fsm in fs_map['filesystems']:
                fs = fsm['mdsmap']
                report['fs']['filesystems'].append({
                    'max_mds': fs['max_mds'],
                    'ever_allowed_features': fs['ever_allowed_features'],
                    'explicitly_allowed_features': fs['explicitly_allowed_features'],
                    'num_in': len(fs['in']),
                    'num_up': len(fs['up']),
                    'num_standby_replay': len(
                        [mds for gid, mds in fs['info'].items()
                         if mds['state'] == 'up:standby-replay']),
                    'num_mds': len(fs['info']),
                    'balancer_enabled': len(fs['balancer']) > 0,
                    'num_data_pools': len(fs['data_pools']),
                    'standby_count_wanted': fs['standby_count_wanted'],
                    'approx_ctime': fs['created'][0:7],
                })
                num_mds += len(fs['info'])
            report['fs']['total_num_mds'] = num_mds

            # daemons
            report['metadata'] = dict()
            report['metadata']['osd'] = self.gather_osd_metadata(osd_map)
            report['metadata']['mon'] = self.gather_mon_metadata(mon_map)

            # host counts
            servers = self.list_servers()
            self.log.debug('servers %s' % servers)
            report['hosts'] = {
                'num': len([h for h in servers if h['hostname']]),
            }
            for t in ['mon', 'mds', 'osd', 'mgr']:
                report['hosts']['num_with_' + t] = len(
                    [h for h in servers
                     if len([s for s in h['services'] if s['type'] == t])]
                )

            report['usage'] = {
                'pools': len(df['pools']),
                'pg_num:': num_pg,
                'total_used_bytes': df['stats']['total_used_bytes'],
                'total_bytes': df['stats']['total_bytes'],
                'total_avail_bytes': df['stats']['total_avail_bytes']
            }

            report['services'] = defaultdict(int)
            for key, value in service_map['services'].items():
                report['services'][key] += 1

            try:
                report['balancer'] = self.remote('balancer', 'gather_telemetry')
            except ImportError:
                report['balancer'] = {
                    'active': False
                }

        if 'crash' in channels:
            report['crashes'] = self.gather_crashinfo()

        if 'device' in channels:
            report['devices'] = self.gather_device_report()

        return report

    def send(self, report):
        self.log.info('Upload report to: %s', self.url)
        proxies = dict()
        if self.proxy:
            self.log.info('Using HTTP(S) proxy: %s', self.proxy)
            proxies['http'] = self.proxy
            proxies['https'] = self.proxy

        resp = requests.put(url=self.url, json=report, proxies=proxies)
        if not resp.ok:
            self.log.error("Report send failed: %d %s %s" %
                           (resp.status_code, resp.reason, resp.text))
        return resp

    def handle_command(self, inbuf, command):
        if command['prefix'] == 'telemetry status':
            r = {}
            for opt in self.MODULE_OPTIONS:
                r[opt['name']] = getattr(self, opt['name'])
            return 0, json.dumps(r, indent=4), ''
        elif command['prefix'] == 'telemetry on':
            if command.get('license') != LICENSE:
                return -errno.EPERM, '', "Telemetry data is licensed under the " + LICENSE_NAME + " (" + LICENSE_URL + ").\nTo enable, add '--license " + LICENSE + "' to the 'ceph telemetry on' command."
            self.set_module_option('enabled', True)
            self.set_module_option('last_opt_revision', REVISION)
            return 0, '', ''
        elif command['prefix'] == 'telemetry off':
            self.set_module_option('enabled', False)
            self.set_module_option('last_opt_revision', REVISION)
            return 0, '', ''
        elif command['prefix'] == 'telemetry send':
            self.last_report = self.compile_report()
            resp = self.send(self.last_report)
            if resp.ok:
                return 0, 'Report sent to {0}'.format(self.url), ''
            return 1, '', 'Failed to send report to %s: %d %s %s' % (
                self.url,
                resp.status_code,
                resp.reason,
                resp.text
            )

        elif command['prefix'] == 'telemetry show':
            report = self.compile_report(
                channels=command.get('channels', None)
            )
            return 0, json.dumps(report, indent=4), ''
        else:
            return (-errno.EINVAL, '',
                    "Command not found '{0}'".format(command['prefix']))

    def self_test(self):
        report = self.compile_report()
        if len(report) == 0:
            raise RuntimeError('Report is empty')

        if 'report_id' not in report:
            raise RuntimeError('report_id not found in report')

    def shutdown(self):
        self.run = False
        self.event.set()

    def refresh_health_checks(self):
        health_checks = {}
        if self.enabled and self.last_opt_revision < LAST_REVISION_RE_OPT_IN:
            health_checks['TELEMETRY_CHANGED'] = {
                'severity': 'warning',
                'summary': 'Telemetry requires re-opt-in',
                'detail': [
                    'telemetry report includes new information; must re-opt-in (or out)'
                ]
            }
        self.set_health_checks(health_checks)

    def serve(self):
        self.load()
        self.config_notify()
        self.run = True

        self.log.debug('Waiting for mgr to warm up')
        self.event.wait(10)

        while self.run:
            self.event.clear()

            self.refresh_health_checks()

            if self.last_opt_revision < LAST_REVISION_RE_OPT_IN:
                self.log.debug('Not sending report until user re-opts-in')
                self.event.wait(1800)
                continue
            if not self.enabled:
                self.log.debug('Not sending report until configured to do so')
                self.event.wait(1800)
                continue

            now = int(time.time())
            if not self.last_upload or (now - self.last_upload) > \
                            self.interval * 3600:
                self.log.info('Compiling and sending report to %s',
                              self.url)

                try:
                    self.last_report = self.compile_report()
                except:
                    self.log.exception('Exception while compiling report:')

                try:
                    resp = self.send(self.last_report)
                    # self.send logs on failure; only update last_upload
                    # if we succeed
                    if resp.ok:
                        self.last_upload = now
                        self.set_store('last_upload', str(now))
                except:
                    self.log.exception('Exception while sending report:')
            else:
                self.log.debug('Interval for sending new report has not expired')

            sleep = 3600
            self.log.debug('Sleeping for %d seconds', sleep)
            self.event.wait(sleep)

    def self_test(self):
        self.compile_report()
        return True

    @staticmethod
    def can_run():
        return True, ''
