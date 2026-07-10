param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$BusId = "",
    [string]$HostBind = "0.0.0.0",
    [int]$Port = 8766,
    [switch]$AllowSms
)

$ErrorActionPreference = "Stop"

function Quote-Bash {
    param([string]$Value)
    return "'" + ($Value -replace "'", "'\''") + "'"
}

function Expand-LiteralTemplate {
    param(
        [string]$Template,
        [hashtable]$Values
    )

    foreach ($key in $Values.Keys) {
        $Template = $Template.Replace("@@$key@@", [string]$Values[$key])
    }
    return $Template
}

function Invoke-Wsl {
    param([string]$Command)
    & wsl.exe -d $Distro -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE"
    }
}

function Invoke-WslInteractive {
    param([string]$Command)

    & wsl.exe -d $Distro -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Interactive WSL command failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Usbipd {
    param([string[]]$Arguments)
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & usbipd.exe @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $outputText = ($output | ForEach-Object { $_.ToString() }) -join "`n"
    $output | ForEach-Object { Write-Host $_.ToString() }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $outputText
    }
}

function Resolve-DjiBusId {
    param([string]$OverrideBusId)

    if ($OverrideBusId) {
        return $OverrideBusId
    }

    $listResult = Invoke-Usbipd -Arguments @("list")
    if ($listResult.ExitCode -ne 0) {
        throw "usbipd list failed with exit code $($listResult.ExitCode)"
    }

    $targetLine = $listResult.Output -split "`n" | Where-Object { $_ -match "\b2CA3:4006\b" } | Select-Object -First 1
    if (-not $targetLine) {
        throw "DJI/Baiwang USB device 2CA3:4006 was not found in usbipd list. Replug the module, then retry."
    }

    if ($targetLine -notmatch "^\s*(\S+)\s+") {
        throw "Unable to parse BusId from usbipd line: $targetLine"
    }

    return $Matches[1]
}

function Invoke-UsbipdBind {
    param([string]$TargetBusId)
    $listResult = Invoke-Usbipd -Arguments @("list")
    if ($listResult.ExitCode -ne 0) {
        throw "usbipd list failed with exit code $($listResult.ExitCode)"
    }

    $targetLine = $listResult.Output -split "`n" | Where-Object { $_ -match "^\s*$([regex]::Escape($TargetBusId))\s+" } | Select-Object -First 1
    if ($targetLine -match "\b(Attached|Shared)\b") {
        Write-Host "usbipd device $TargetBusId is already shared or attached; skipping bind."
        return
    }

    $result = Invoke-Usbipd -Arguments @("bind", "--busid", $TargetBusId)
    if ($result.ExitCode -ne 0 -and ($result.Output -notmatch "already|shared|attached|client|busy")) {
        throw "usbipd bind failed with exit code $($result.ExitCode). Run this script from an elevated PowerShell once, or run: usbipd bind --busid $TargetBusId"
    }
}

function Invoke-UsbipdAttach {
    param([string]$TargetBusId)
    $result = Invoke-Usbipd -Arguments @("attach", "--wsl", "--busid", $TargetBusId)
    if ($result.ExitCode -ne 0 -and ($result.Output -notmatch "already attached|already|attached|busy|client")) {
        throw "usbipd attach failed with exit code $($result.ExitCode)"
    }
}

function Invoke-WslCapture {
    param([string]$Command)
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & wsl.exe -d $Distro -- bash -lc $Command 2>&1
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($LASTEXITCODE -ne 0) {
        $message = ($output | ForEach-Object { $_.ToString() }) -join "`n"
        throw "WSL command failed with exit code $LASTEXITCODE`n$message"
    }
    return ($output | Select-Object -Last 100 | ForEach-Object { $_.ToString() }) -join "`n"
}

function Invoke-WslScriptCapture {
    param([string]$Script)

    $scriptBytes = [System.Text.Encoding]::UTF8.GetBytes($Script)
    $scriptBase64 = [Convert]::ToBase64String($scriptBytes)
    $scriptBase64Q = Quote-Bash $scriptBase64
    $command = "printf '%s' $scriptBase64Q | base64 -d | bash"

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & wsl.exe -d $Distro -- bash -lc $command 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        $message = ($output | ForEach-Object { $_.ToString() }) -join "`n"
        throw "WSL script failed with exit code $exitCode`n$message"
    }
    return ($output | Select-Object -Last 100 | ForEach-Object { $_.ToString() }) -join "`n"
}

function Assert-CardPulseWebPort {
    param([int]$PortNumber)

    $listener = $null
    for ($attempt = 0; $attempt -lt 10; $attempt += 1) {
        $listener = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $listener) {
            return
        }
        Start-Sleep -Milliseconds 250
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    $owner = if ($process) {
        "$($process.Name) PID $($process.ProcessId): $($process.CommandLine)"
    } else {
        "PID $($listener.OwningProcess)"
    }
    throw "Port $PortNumber is already used by $owner. Stop that process or Use -Port with another local port."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRootQ = Quote-Bash $repoRoot
$repoWsl = (& wsl.exe -d $Distro -- bash -lc "wslpath -a $repoRootQ" | Select-Object -Last 1).Trim()
if (-not $repoWsl) {
    throw "Unable to resolve repository path inside WSL."
}

$wslHome = (& wsl.exe -d $Distro -- bash -lc 'printf "%s" "$HOME"' | Select-Object -Last 1).Trim()
if (-not $wslHome) {
    throw "Unable to resolve WSL home directory."
}

$configDir = "$wslHome/.cardpulse-dji/config"
$stateDir = "$wslHome/.cardpulse-dji/state"
$recoveryStatePath = "$stateDir/recovery.json"
$recoveryTempPath = "$stateDir/recovery.json.tmp"
$repoQ = Quote-Bash $repoWsl
$configDirQ = Quote-Bash $configDir
$stateDirQ = Quote-Bash $stateDir
$recoveryStatePathQ = Quote-Bash $recoveryStatePath
$recoveryTempPathQ = Quote-Bash $recoveryTempPath

function Write-RecoveryState {
    param(
        [string]$State,
        [string]$Step,
        [string]$PhaseStatus,
        [string]$Summary,
        [string]$OperatorHint,
        [string]$PortValue = "",
        [string]$WebUrl = "",
        [string]$BusIdValue = "",
        [string]$DistroValue = $Distro
    )

    $StateSafe = if ($null -eq $State) { "" } else { $State }
    $StepSafe = if ($null -eq $Step) { "" } else { $Step }
    $PhaseStatusSafe = if ($null -eq $PhaseStatus) { "" } else { $PhaseStatus }
    $SummarySafe = if ($null -eq $Summary) { "" } else { $Summary }
    $OperatorHintSafe = if ($null -eq $OperatorHint) { "" } else { $OperatorHint }
    $PortSafe = if ($null -eq $PortValue) { "" } else { $PortValue }
    $WebUrlSafe = if ($null -eq $WebUrl) { "" } else { $WebUrl }
    $BusIdSafe = if ($null -eq $BusIdValue) { "" } else { $BusIdValue }
    $DistroSafe = if ($null -eq $DistroValue) { "" } else { $DistroValue }
    $checkedAt = [DateTimeOffset]::UtcNow.ToString("o")
    $payload = [ordered]@{
        state = $StateSafe
        step = $StepSafe
        phase_status = $PhaseStatusSafe
        summary = $SummarySafe
        operator_hint = $OperatorHintSafe
        checked_at = $checkedAt
        port = $PortSafe
        web_url = $WebUrlSafe
        busid = $BusIdSafe
        distro = $DistroSafe
    }
    $json = $payload | ConvertTo-Json -Depth 3
    $jsonBytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $jsonBase64 = [Convert]::ToBase64String($jsonBytes)
    $jsonBase64Q = Quote-Bash $jsonBase64
    $writeScript = "set -euo pipefail; mkdir -p $stateDirQ; printf '%s' $jsonBase64Q | base64 -d > $recoveryTempPathQ; mv $recoveryTempPathQ $recoveryStatePathQ"
    & wsl.exe -d $Distro -- bash -lc $writeScript | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to write recovery state with exit code $LASTEXITCODE"
    }
}

function Set-RecoveryStage {
    param(
        [string]$Step,
        [string]$PhaseStatus,
        [string]$Summary,
        [string]$OperatorHint = "",
        [string]$PortValue = "",
        [string]$WebUrl = "",
        [string]$BusIdValue = ""
    )

    $script:CurrentRecoveryStep = $Step
    $script:CurrentRecoveryHint = $OperatorHint
    Write-RecoveryState -State "running" -Step $Step -PhaseStatus $PhaseStatus -Summary $Summary -OperatorHint $OperatorHint -PortValue $PortValue -WebUrl $WebUrl -BusIdValue $BusIdValue -DistroValue $Distro
}

$CurrentRecoveryStep = "init"
$CurrentRecoveryHint = ""
$TargetBusId = ""

try {
Set-RecoveryStage -Step "web-port" -PhaseStatus "checking" -Summary "Checking local Web port" -OperatorHint "Stop the conflicting process or choose another -Port value."
Invoke-Wsl -Command "pkill -f '[s]cripts/cardpulse-web.py' 2>/dev/null || true"
Assert-CardPulseWebPort -PortNumber $Port

Write-Host "[1/7] Keeping WSL distro alive: $Distro"
Set-RecoveryStage -Step "wsl" -PhaseStatus "running" -Summary "Keeping WSL distro alive"
Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "--", "bash", "-lc", "while true; do sleep 3600; done") -WindowStyle Hidden
Start-Sleep -Seconds 1

Set-RecoveryStage -Step "usbipd" -PhaseStatus "running" -Summary "Finding DJI/Baiwang USB device" -OperatorHint "If this fails, replug the module and make sure usbipd can see 2CA3:4006." -BusIdValue $BusId
$TargetBusId = Resolve-DjiBusId -OverrideBusId $BusId
Write-Host "[2/7] Sharing and attaching DJI/Baiwang USB device via usbipd: $TargetBusId"
Write-Host "Using DJI/Baiwang USB BusId: $TargetBusId"
Set-RecoveryStage -Step "usbipd" -PhaseStatus "running" -Summary "Sharing and attaching DJI/Baiwang USB device" -OperatorHint "If this fails, replug the module and make sure usbipd can see 2CA3:4006." -BusIdValue $TargetBusId
Invoke-UsbipdBind -TargetBusId $TargetBusId
Invoke-UsbipdAttach -TargetBusId $TargetBusId

Write-Host "[3/7] Preparing stable CardPulse config in WSL: $configDir"
Set-RecoveryStage -Step "config" -PhaseStatus "running" -Summary "Preparing stable CardPulse config" -BusIdValue $TargetBusId
$configScript = @"
set -euo pipefail
mkdir -p $configDirQ $stateDirQ
if [ ! -f $configDirQ/config.yaml ]; then
  cat > $configDirQ/config.yaml <<'YAML'
serial:
  port: /dev/ttyUSB2
  baudrate: 115200
  auto_detect: false
sms:
  phone: +8613800138000
  message: CardPulse DJI test
  interval_days: 179
  timeout: 30
retry:
  max_attempts: 1
  interval: 1
notify:
  enabled: false
YAML
fi
chmod 600 $configDirQ/config.yaml
"@
Invoke-Wsl -Command $configScript

Write-Host "[4/7] Binding DJI/Baiwang module to Linux option serial driver"
Set-RecoveryStage -Step "driver" -PhaseStatus "running" -Summary "Binding DJI/Baiwang module to Linux option serial driver" -OperatorHint "Enter the Ubuntu sudo password when prompted; it is never stored by this script." -BusIdValue $TargetBusId
Write-Host "WSL may prompt for the Ubuntu sudo password; it is used only by sudo."
Invoke-WslInteractive -Command "cd $repoQ && sudo bash scripts/dji-qdc507-wsl-prepare.sh"

Write-Host "[5/7] Verifying AT serial path and doctor output"
Set-RecoveryStage -Step "doctor" -PhaseStatus "running" -Summary "Verifying AT serial path and doctor output" -OperatorHint "If this fails, check that /dev/ttyUSB* exists and one port answers AT." -BusIdValue $TargetBusId
$detectScriptTemplate = @'
set -euo pipefail
CONFIG_DIR=@@CONFIG_DIR_Q@@
cd @@REPO_Q@@
candidates=$(ls /dev/ttyUSB* 2>/dev/null || true)
if [ -z "$candidates" ]; then
  echo "[ERROR] No /dev/ttyUSB* port found after driver bind." >&2
  exit 21
fi

update_config_port() {
  local device="$1"
  DEVICE_PORT="$device" CONFIG_DIR="$CONFIG_DIR" python3 - <<'PY'
from pathlib import Path
import os

path = Path(os.environ["CONFIG_DIR"]) / "config.yaml"
text = path.read_text(encoding="utf-8")
if len(text.splitlines()) <= 1 and "\\n" in text:
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
lines = []
in_serial = False
for line in text.splitlines():
    stripped = line.strip()
    if stripped == "serial:":
        in_serial = True
        lines.append(line)
        continue
    if in_serial and line.startswith((" ", "\t")) and stripped.startswith("port:"):
        indent = line[: len(line) - len(line.lstrip())]
        lines.append(f"{indent}port: {os.environ['DEVICE_PORT']}")
        continue
    if stripped and not line.startswith((" ", "\t")):
        in_serial = False
    lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

doctor_output=""
selected_device=""
for device in $candidates; do
  update_config_port "$device"
  current_output=$(env CARDPULSE_CONFIG_DIR=@@CONFIG_DIR_Q@@ CARDPULSE_STATE_DIR=@@STATE_DIR_Q@@ bash bin/cardpulse --doctor 2>&1 || true)
  actual_device=$(printf '%s\n' "$current_output" | sed -nE 's/^[[:space:]]*device:[[:space:]]*([^[:space:]]+)[[:space:]]*$/\1/p' | tail -n 1)
  printf '%s\n' "Trying AT serial port: $device"
  printf '%s\n' "$current_output"
  if printf '%s' "$current_output" | grep -q "AT: OK" &&
     printf '%s' "$current_output" | grep -q "SIM: READY" &&
     printf '%s\n' "$current_output" | grep -Eq "^[[:space:]]*RSSI: ([0-9]|[1-8][0-9]|9[0-8])[[:space:]]*$" &&
     printf '%s\n' "$current_output" | grep -Eq "^[[:space:]]*Network registration: (1|5)[[:space:]]*$"; then
    selected_device="${actual_device:-$device}"
    doctor_output="$current_output"
    break
  fi
done

if [ -z "$selected_device" ]; then
  echo "[ERROR] cardpulse --doctor did not confirm AT: OK, SIM: READY, RSSI != 99, and Network registration: 1/5 on any /dev/ttyUSB* candidate." >&2
  exit 22
fi

printf '%s\n' "Detected AT serial port: $selected_device"
printf '%s\n' "$doctor_output"
'@
$detectScript = (Expand-LiteralTemplate -Template $detectScriptTemplate -Values @{
    CONFIG_DIR_Q = $configDirQ
    REPO_Q = $repoQ
    STATE_DIR_Q = $stateDirQ
}).Replace("`r`n", "`n")
$doctorOutput = Invoke-WslScriptCapture -Script $detectScript
if ($doctorOutput -notmatch "Detected AT serial port") {
    throw "Unable to determine detected AT serial port.`n$doctorOutput"
}
if ($doctorOutput -notmatch "AT: OK") {
    throw "cardpulse --doctor did not confirm AT: OK.`n$doctorOutput"
}
if ($doctorOutput -notmatch "SIM: READY") {
    throw "cardpulse --doctor did not confirm SIM: READY.`n$doctorOutput"
}
if ($doctorOutput -notmatch "(?m)^\s*RSSI: ([0-9]|[1-8][0-9]|9[0-8])\s*$") {
    throw "cardpulse --doctor did not confirm usable RSSI.`n$doctorOutput"
}
if ($doctorOutput -notmatch "(?m)^\s*Network registration: (1|5)\s*$") {
    throw "cardpulse --doctor did not confirm network registration 1/5.`n$doctorOutput"
}
Write-Host $doctorOutput
$detectedPort = ([regex]::Match($doctorOutput, "Detected AT serial port:\s*(\S+)")).Groups[1].Value

Write-Host "[6/7] Starting CardPulse Web on http://127.0.0.1:$Port"
Set-RecoveryStage -Step "web" -PhaseStatus "starting" -Summary "Starting CardPulse Web" -PortValue $detectedPort -WebUrl "http://127.0.0.1:$Port" -BusIdValue $TargetBusId
$allowSmsArg = if ($AllowSms) { " --allow-sms" } else { "" }
$webCommand = "cd $repoQ; exec env CARDPULSE_CONFIG_DIR=$configDirQ CARDPULSE_STATE_DIR=$stateDirQ python3 scripts/cardpulse-web.py --host $HostBind --port $Port$allowSmsArg >> /tmp/cardpulse-web.log 2>&1"
Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "--", "bash", "-lc", $webCommand) -WindowStyle Hidden
Start-Sleep -Seconds 2

Write-Host "[7/7] Verifying Web and modem status"
Set-RecoveryStage -Step "web" -PhaseStatus "checking" -Summary "Verifying Web and modem status" -PortValue $detectedPort -WebUrl "http://127.0.0.1:$Port" -BusIdValue $TargetBusId
$health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 10
$info = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/info" -TimeoutSec 35
$overview = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/overview" -TimeoutSec 45

$summary = [ordered]@{
    web_url = "http://127.0.0.1:$Port"
    sms_enabled = $health.sms_enabled
    overview_status = $overview.overall_status
    recommended_action = $overview.recommended_action
    sim = $info.sim
    signal = $info.signal
    operator = $info.operator
    imei = $info.imei
    config = "$configDir/config.yaml"
}

[pscustomobject]$summary | Format-List

if ($info.sim -ne "READY") {
    throw "Web /api/info did not report SIM READY."
}
if (-not ($info.signal -match "^\d+$") -or [int]$info.signal -eq 99) {
    throw "Web /api/info did not report usable RSSI."
}
if (-not $overview.ok) {
    throw "Web /api/overview did not report a healthy overview: $($overview.recommended_action)"
}

$webUrl = "http://127.0.0.1:$Port"
Write-RecoveryState -State "ok" -Step "web" -PhaseStatus "complete" -Summary "WSL 恢复成功" -OperatorHint "" -PortValue $detectedPort -WebUrl $webUrl -BusIdValue $TargetBusId -DistroValue $Distro

if (-not $AllowSms) {
    Write-Host "SMS test remains disabled. Start with -AllowSms only when you intentionally want the guarded SMS test endpoint."
}
} catch {
    $errorMessage = $_.Exception.Message
    try {
        Write-RecoveryState -State "error" -Step $CurrentRecoveryStep -PhaseStatus "failed" -Summary $errorMessage -OperatorHint $CurrentRecoveryHint -BusIdValue $TargetBusId -DistroValue $Distro
    } catch {
    }
    throw
}
