Because the WD 4.0 client will a complete reimplementation of WD 3.x, I have chosen to create this new repo for 4.x

ClaudeAI has reviewed the WD 3.x github files, so I am starting wby defining the different requirements and features of 4.0

I'm not yet sure if Claude fully understands the processing chain, so I will give it more of those details in the hope that few of them get lost in the alpha code it produces

I have a test Beelink with two attached RX888s and four KiwiSDRs which I will use for beta testing.

So don't try to run any of the code which will be appearing in this project until I am ready for beta testing

## HamSCI client contract

Targets contract **v0.4** (see
`https://github.com/mijahauan/sigmond/blob/main/docs/CLIENT-CONTRACT.md`).
Conformance surfaces:

- `wd-ctl inventory --json`, `wd-ctl validate --json`, `wd-ctl version` —
  self-describe CLI (§3).
- `deploy.toml` at repo root — deploy manifest (§5).
- `EnvironmentFile=-/etc/sigmond/coordination.env` on every unit (§4).
- File logs under `/var/log/wsprdaemon/` surfaced as `log_paths` (§10).
- Runtime log level via `WSPRDAEMON_LOG_LEVEL` or `CLIENT_LOG_LEVEL`,
  re-read on SIGHUP (§11).
- `validate` hardening: entry-point reachability (§12.1), SSRC uniqueness
  per receiver (§12.2), `config_path` disclosure (§12.3), ka9q-python
  version-floor warning (§12.6).

### Pattern A repo layout (§12.5)

Canonical install path: **`/opt/git/wsprdaemon-client`**, owned
`<maintainer>:<service-group>`, group-writable, with a convenience symlink
`~/wsprdaemon-client → /opt/git/wsprdaemon-client`. The service user must
be a member of the service group. The reverse anti-pattern (`install.sh`
linking `/opt/git/<name> → ~/…`) fails the mode-700 home-traversability
check and is not supported.

