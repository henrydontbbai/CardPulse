# CardPulse fnOS Package

`scripts/build-fnos-fpk.sh` creates the manual-upload `.fpk` with a real
`linux/amd64` OCI image digest. It refuses tags and unresolved image names.

```bash
CARDPULSE_IMAGE='registry.example/cardpulse@sha256:<64-hex-digest>' \
  sh scripts/build-fnos-fpk.sh
```

The output is a unique file such as
`packaging/fnos/dist/cardpulse-1.1.1-sha256-<64-hex-digest>.fpk`. Use the exact
path printed by the build script, rather than an older file in `dist/`, when
uploading to fnOS App Center. The build checks `CardPulse --version` from the
exact image digest and requires it to match the FPK version. The packaged
Compose environment binds that same digest and FPK version to the QDC507
read-only acceptance marker. Before enabling the scheduler, perform the
QDC507 read-only acceptance on `/dev/cardpulse-at`.

`packaging/fnos/dist/cardpulse-1.1.0.fpk` is a historical artifact, not a
release candidate. It predates the current gateway, lifecycle, default
no-device, and digest-pinning contracts, and `scripts/verify-fnos-fpk.py`
rejects it. Do not upload it; leave it in place for audit history.

Enabling the fixed device overlay only makes `/dev/cardpulse-at` available for
the read-only acceptance POC. It never enables automatic sending. The
scheduler remains disabled until that POC records its acceptance marker and a
fnOS administrator explicitly enables it from the Web settings page.

Before a QDC507 has a host-prepared AT serial device, use the packaged
`diagnostics/fnos-platform-poc.sh` only in an authorized maintenance window.
Its `--record-persistence` marker verifies private-volume persistence but does
not count as QDC507 acceptance or gateway authorization. The direct Unix-socket
check is backend reachability; the real HTTPS gateway administrator,
normal-user, and forged-header checks remain a NAS 56 acceptance requirement.

fnOS write requests require the HTTPS gateway to inject the administrator and
forwarded-origin headers. During the 56 NAS acceptance, verify that a
non-administrator request, including one that supplies forged `X-Trim-Isadmin`
or `X-Forwarded-*` headers, cannot load the application or obtain a CSRF token.

NAS 56 now has a separately administered host pre-provisioning result: the
QDC507 is `2ca3:4006`, its verified AT interface is `/dev/ttyUSB3`, and
`/dev/cardpulse-at` resolves to that `root:dialout 0660` device. Because this
kernel has neither an in-tree alias nor a persistent module parameter for the
USB ID, the host administrator maintains a root-owned
`/usr/local/lib/cardpulse/qdc507-option-bind` helper and matching USB-add udev
rule outside this package. They only load `option` and register `new_id`.

Those host files are not FPK assets: the build script does not copy them, and
FPK install, upgrade, uninstall, or runtime code must never create, modify, or
invoke them. A post-helper physical replug has restored all five `option` TTYs,
the fixed alias, and the read-only AT/SIM/network checks. Do not enable the
device overlay or count the host pre-provisioning as QDC507 acceptance until
non-root container access and the packaged read-only device POC have succeeded.

The PNG icons are temporary fnpack-template assets and must be replaced with
approved CardPulse branding before a public release.
