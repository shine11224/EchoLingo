# Baidu Drive setup

EchoLingo does not collect or synchronize a user's Baidu Drive credentials. Each user authorizes `bdpan` only on the computer that runs EchoLingo.

## One-click setup

Open **Learning resources → Baidu Drive setup**. On first use EchoLingo shows all installation metadata before it changes the computer:

- source: the [official Baidu Drive `bdpan-storage` project](https://github.com/baidu-netdisk/bdpan-storage)
- pinned CLI installer version
- detected platform and installation location
- exact official CDN URL and SHA-256 checksum

Select **Confirm and install**. EchoLingo downloads only the pinned installer, enforces its SHA-256 checksum, runs the official non-interactive installer, and detects the installed executable without requiring a `PATH` or `.env` change. Installation never starts automatically on page load.

After installation the Baidu authorization page opens automatically. Approve access, return to EchoLingo, paste the 32-character authorization code, and select **Complete authorization**. The code is sent to `bdpan` through standard input and is not exposed in process arguments.

If `bdpan` is already installed, the page skips installation and shows **Start authorization**.

## Manual fallback

The web flow is the default. If the detected platform is not supported by the pinned official installer, install `bdpan` from the official project, then use its safe stdin authorization flow:

```powershell
bdpan login --accept-disclaimer --get-auth-url
<authorization-code> | bdpan login --accept-disclaimer --set-code-stdin
```

Verify the result without printing tokens:

```powershell
bdpan whoami
```

Return to EchoLingo and select **Recheck**. When the status changes to configured, the Baidu Drive import card can accept a share link or browse files in the app data directory.

## Security and scope

- OAuth credentials remain in the local `bdpan` configuration. Never commit or share that configuration.
- EchoLingo does not ask for a Baidu password or browser cookie.
- The integration works within Baidu Drive's app data scope (`/apps/bdpan/`).
- Installer download and execution endpoints accept requests only from the loopback interface.
- If the token expires, repeat the in-app authorization flow and select **Recheck**.

## Troubleshooting

- **`bdpan` not installed** — use **Confirm and install** in the settings page.
- **Checksum failure** — do not bypass it; refresh the project or wait for an updated pinned installer manifest.
- **Not logged in or token expired** — select **Start authorization** again.
- **Settings still show the old status** — select **Recheck**; the capability probe is cached briefly.
- **Share import fails** — confirm it is a single-file share, add the extraction code when required, and keep the file at or below 1 GB.
