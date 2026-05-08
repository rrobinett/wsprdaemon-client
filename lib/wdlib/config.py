"""wsprdaemon v4 configuration data model."""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Receiver:
    """A receiver defined in wsprdaemon.conf."""
    name: str
    address: str       # IP:port or multicast DNS name
    call: str
    grid: str
    password: str      # 'NULL' or actual password
    locality: str = ''      # 'local' (radiod on this host) | 'remote' (radiod elsewhere)
    radiod_name: str = ''   # e.g. 'k3lr-rx888'; status DNS = <radiod_name>-status.local

    @property
    def receiver_type(self) -> str:
        """Infer receiver type from name prefix."""
        upper = self.name.upper()
        if upper.startswith('KA9Q_') and '_WWV' in upper:
            return 'ka9q_wwv'
        elif upper.startswith('KA9Q_'):
            return 'ka9q'
        elif upper.startswith('KIWI_'):
            return 'kiwi'
        elif upper.startswith('MERG_'):
            return 'merge'
        else:
            return 'unknown'

    @property
    def is_merge(self) -> bool:
        return self.receiver_type == 'merge'

    @property
    def merge_sources(self) -> List[str]:
        """For merge receivers, parse the comma-separated source list."""
        if not self.is_merge:
            return []
        return [s.strip() for s in self.address.split(',')]


@dataclass
class ScheduleEntry:
    """A single receiver+band+modes assignment in a schedule slot."""
    receiver: str
    band: str
    modes: str      # Colon-separated, e.g. "W2:F2:F5"


@dataclass
class ScheduleSlot:
    """A time slot in the schedule."""
    time: str       # "HH:MM" or "00:00" for always-on
    entries: List[ScheduleEntry] = field(default_factory=list)


@dataclass
class Ka9qWebConfig:
    """Configuration for the ka9q-web peer service (radiod web status UI).

    One instance is deployed per unique local KA9Q radiod.
    Ports are assigned starting at base_port (default 8081), incrementing by 1
    per radiod in stable name-sorted order.
    """
    enabled: bool = False
    base_port: int = 8081     # first instance port; successive instances = +1


@dataclass
class HfTimestdConfig:
    """Configuration for the hf-timestd peer service (WWV/CHU IQ recording + UTC timing)."""
    enabled: bool = False
    timing_authority: str = 'rtp'    # rtp | fusion | auto
    compression: str = 'zstd'        # none | zstd | lz4
    uploader_enabled: bool = False
    physics_enabled: bool = False    # enable ionospheric physics pipeline
                                     # (timestd-physics, ionex-download, iono-reanalysis)
                                     # requires a capable CPU; disable on weak hardware
    storage_quota: str = '70%'       # delete oldest data when disk exceeds this %
    archive_path: str = ''           # optional path (or symlink) to external drive for
                                     # long-term raw IQ archive; leave blank to disable


@dataclass
class WdConfig:
    """Complete parsed wsprdaemon configuration."""
    receivers: Dict[str, Receiver] = field(default_factory=dict)
    schedule_slots: List[ScheduleSlot] = field(default_factory=list)
    ka9q_conf_name: str = ''
    ka9q_web_dns: str = ''
    rac: str = ''
    # Operator identity defaults from [general]; per-receiver call/grid
    # override these.  When [general] also leaves them empty, the parser
    # falls back to STATION_CALL / STATION_GRID env vars (published by
    # sigmond from /etc/sigmond/coordination.toml [host]).
    reporter_call: str = ''
    reporter_grid: str = ''
    hf_timestd: HfTimestdConfig = field(default_factory=HfTimestdConfig)
    ka9q_web: Ka9qWebConfig = field(default_factory=Ka9qWebConfig)
    # Space-separated CPU IDs reserved for manually-managed services (e.g. a second
    # radiod instance not managed by wd-ctl).  These CPUs are excluded from the
    # worker-CPU mask applied to all wd-ctl-managed services.
    reserved_cpus: str = ''

    # Remote Access Channel — frpc tunnel to wsprdaemon gateway.
    # rac:                channel number (integer); derives remote port = 35800 + rac
    # rac_token:          shared secret for frps-secure authentication (port 35736)
    # rac_server:         primary frps host  (default: remote.wsprdaemon.org)
    # rac_fallback_server: optional fallback frps host if primary unreachable
    rac_token:           str = ''
    rac_server:          str = 'remote.wsprdaemon.org'
    rac_fallback_server: str = ''
    rac_tls_ca:          str = ''   # path to server CA cert for self-signed TLS verification

    # Derived data computed after parsing
    band_modes: Dict[str, set] = field(default_factory=dict)
    receiver_bands: Dict[str, set] = field(default_factory=dict)
