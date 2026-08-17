# Baidu Drive setup

EchoLingo does not ship, collect or synchronize a user's Baidu Drive credentials. Each user must install and authorize `bdpan` on the computer that runs EchoLingo.

## 1. Install the CLI

Follow the [official Baidu Drive tool instructions](https://github.com/baidu-netdisk/bdpan-storage) and install `bdpan` 3.8 or newer. Confirm that the executable is available:

```powershell
bdpan --version
```

If it is not on `PATH`, set the full executable path in `.env`:

```dotenv
ELT_BAIDU_PAN_BIN=C:\path\to\bdpan.exe
```

## 2. Authorize the current user

Run the first command, open the returned URL in a browser, and approve access with the Baidu account that should be used by EchoLingo:

```powershell
bdpan login --accept-disclaimer --get-auth-url
```

Copy the 32-character authorization code and complete login:

```powershell
bdpan login --accept-disclaimer --set-code <authorization-code>
```

Verify the result without printing tokens:

```powershell
bdpan whoami
```

Return to EchoLingo → Settings → Baidu Drive and select **Recheck**. When the status changes to configured, the Baidu Drive import card can accept a share link or browse files in the app data directory.

## Security and scope

- OAuth credentials remain in the local `bdpan` configuration. Never commit or share that configuration.
- EchoLingo does not ask for a Baidu password or browser cookie.
- The integration works within Baidu Drive's app data scope (`/apps/bdpan/`).
- If the token expires, repeat the authorization steps and select **Recheck**.

## Troubleshooting

- **`bdpan` not installed** — add the executable to `PATH` or set `ELT_BAIDU_PAN_BIN`.
- **Not logged in or token expired** — repeat the two login commands.
- **Settings still show the old status** — select **Recheck**; the capability probe is cached briefly.
- **Share import fails** — confirm it is a single-file share, add the extraction code when required, and keep the file at or below 1 GB.
