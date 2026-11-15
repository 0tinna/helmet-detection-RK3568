<#
.SYNOPSIS
    RK3568 serial command engine (Windows / .NET SerialPort).

.DESCRIPTION
    Reusable base command. One call:
      open port -> Ctrl-C -> answer the board's ESC[6n terminal probe
      -> clear line -> send wrapped command -> capture output between markers
      -> leave the prompt clean -> close port.

    Board quirk: before every prompt it emits  ESC7 ESC[r ESC[999;999H ESC[6n
    and blocks until the terminal reports the cursor position. Bytes sent while
    it is blocked are consumed as the answer, so the engine always replies with
    ESC[24;80R first.

    The command is wrapped as:  echo <TAG>S; <Cmd>; echo <TAG>E rc=$?
    so output boundaries and the exit code are exact.

.PARAMETER Cmd
    Linux shell command (may contain ; && | ). Use single quotes inside.

.PARAMETER Hex
    Send raw hex bytes instead of a command, e.g. "0315" = Ctrl-C + Ctrl-U.

.PARAMETER Timeout
    Seconds to wait for the end marker. Default 20.

.PARAMETER Settle
    Seconds to stay silent before sending. Default 1.0.

.PARAMETER Retry
    Attempts when the end marker never appears. Default 2.

.PARAMETER Raw
    Send Cmd unwrapped and dump everything after Timeout (reboot / long tasks).

.PARAMETER NoInit
    Skip the initial Ctrl-C + DSR sync.

.PARAMETER Quiet
    Suppress [open]/[tx]/[rc]/[closed] lines on stderr.

.EXAMPLE
    rk.cmd "uname -a"
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File serial_console.ps1 -Cmd "df -h" -Timeout 30
#>
param(
    [string]$Port = "COM6",
    [int]$Baud = 1500000,
    [string]$Cmd = "",
    [string]$Hex = "",
    [double]$Timeout = 20,
    [double]$Settle = 1.0,
    [int]$Retry = 2,
    [switch]$Raw,
    [switch]$NoInit,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Info([string]$msg) { if (-not $Quiet) { [Console]::Error.WriteLine($msg) } }

$ESC = [char]27
$DSR_QUERY = "$ESC[6n"
$DSR_REPLY = [byte[]](0x1B, 0x5B, 0x32, 0x34, 0x3B, 0x38, 0x30, 0x52)   # ESC[24;80R

# ---------- text cleanup ----------
function Strip-Ansi([string]$text) {
    if ([string]::IsNullOrEmpty($text)) { return "" }
    $t = $text
    $t = $t -replace '\x1b\[[0-9;?]*[ -/]*[@-~]', ''
    $t = $t -replace '\x1b\][^\x07\x1b]*(\x07|\x1b\\)', ''
    $t = $t -replace '\x1b[()#][0-9A-Za-z]', ''
    $t = $t -replace '\x1b[=>78cMDEHNOZ\\]', ''
    $t = $t -replace '\x1b', ''
    $t = $t -replace '[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ''
    return $t
}

function Normalize-Lines([string]$text) {
    $text = $text -replace "`r`n", "`n" -replace "`r", "`n"
    ($text -split "`n" | ForEach-Object { $_.TrimEnd() }) -join "`n"
}

# ---------- port ----------
$sp = New-Object System.IO.Ports.SerialPort($Port, $Baud, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
$sp.Handshake = [System.IO.Ports.Handshake]::None
$sp.DtrEnable = $true
$sp.RtsEnable = $true
$sp.Encoding = [System.Text.Encoding]::UTF8
$sp.ReadTimeout = 200
$sp.WriteTimeout = 3000

try {
    $sp.Open()
    Write-Info "[open] $Port @ $Baud 8N1"
}
catch {
    Write-Info "[error] cannot open ${Port}: $($_.Exception.Message)"
    exit 2
}

$acc = New-Object System.Text.StringBuilder

function Write-Serial([byte[]]$bytes, [string]$label) {
    $sp.Write($bytes, 0, $bytes.Length)
    if ($label) { Write-Info "[tx] $label ($($bytes.Length) bytes)" }
}

function Read-For([double]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try { $chunk = $sp.ReadExisting() } catch { $chunk = "" }
        if ($chunk -and $chunk.Length -gt 0) { [void]$acc.Append($chunk) }
        else { Start-Sleep -Milliseconds 15 }
    }
}

# Waits for the board's ESC[6n probe and answers it. Repeatable: the probe can
# happen more than once. Returns $true if at least one probe was answered.
function Sync-Terminal([double]$waitSeconds, [int]$maxAnswers = 3) {
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    $seen = ""
    $answered = 0
    while ((Get-Date) -lt $deadline) {
        try { $chunk = $sp.ReadExisting() } catch { $chunk = "" }
        if ($chunk -and $chunk.Length -gt 0) {
            $seen += $chunk
            [void]$acc.Append($chunk)
            if ($seen.Contains($DSR_QUERY)) {
                Start-Sleep -Milliseconds 60
                $sp.Write($DSR_REPLY, 0, $DSR_REPLY.Length)
                $answered++
                $seen = ""
                Write-Info "[sync] answered terminal probe #$answered"
                if ($answered -ge $maxAnswers) { break }
                Start-Sleep -Milliseconds 150
            }
        }
        else { Start-Sleep -Milliseconds 15 }
    }
    return ($answered -gt 0)
}

function Clear-Line {
    # Ctrl-U only: it kills the input line without forcing a new prompt, so no
    # new ESC[6n probe is triggered (a probe would swallow the next command).
    $sp.Write([byte[]]@(0x15), 0, 1)
    Start-Sleep -Milliseconds 120
    $sp.Write([byte[]]@(0x15), 0, 1)
    Start-Sleep -Milliseconds 120
    try { $null = $sp.ReadExisting() } catch { }
    [void]$acc.Clear()
}

try {
    $sp.DiscardInBuffer()
    $sp.DiscardOutBuffer()
    Start-Sleep -Milliseconds 80

    if (-not $NoInit) {
        # Ctrl-C forces the shell to produce a fresh prompt (and thus the probe)
        $sp.Write([byte[]]@(0x03), 0, 1)
        [void](Sync-Terminal 2.0)
        Clear-Line
        Start-Sleep -Milliseconds 200
        try { $null = $sp.ReadExisting() } catch { }
    }

    [void]$acc.Clear()

    # ---------- mode A: raw hex ----------
    if ($Hex -ne "") {
        $clean = ($Hex -replace '[^0-9A-Fa-f]', '')
        if ($clean.Length % 2 -ne 0) { throw "Hex must have an even number of digits" }
        $bytes = New-Object byte[] ([int]($clean.Length / 2))
        for ($i = 0; $i -lt $bytes.Length; $i++) {
            $bytes[$i] = [Convert]::ToByte($clean.Substring($i * 2, 2), 16)
        }
        Write-Serial $bytes (($bytes | ForEach-Object { $_.ToString('X2') }) -join ' ')
        Read-For $Timeout
        [void](Sync-Terminal 0.5)
        [Console]::Out.Write((Normalize-Lines (Strip-Ansi $acc.ToString())))
        exit 0
    }

    # ---------- mode B: listen only ----------
    if ($Cmd -eq "") {
        Read-For $Timeout
        [Console]::Out.Write((Normalize-Lines (Strip-Ansi $acc.ToString())))
        exit 0
    }

    # ---------- mode C: raw send ----------
    if ($Raw) {
        Write-Serial ([System.Text.Encoding]::UTF8.GetBytes($Cmd + "`r")) $Cmd
        Read-For $Timeout
        [void](Sync-Terminal 0.5)
        [Console]::Out.Write((Normalize-Lines (Strip-Ansi $acc.ToString())))
        exit 0
    }

    # ---------- mode D: marker wrapped ----------
    $tag = "__RK" + (Get-Random -Minimum 10000 -Maximum 99999) + "__"
    $beginTag = "${tag}S"
    $endTag = "${tag}E"
    # leading spaces are a sacrificial pad: if the board still eats a few bytes
    # while finishing its terminal probe, it eats blanks instead of the command
    $pad = ' ' * 16
    $wrapped = $pad + ('echo {0}; {1}; echo {2} rc=$?' -f $beginTag, $Cmd, $endTag)
    $donePattern = [regex]::Escape($endTag) + ' rc=\d+'

    $attempts = [Math]::Max(1, $Retry)
    $lastRaw = ""

    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        if ($attempt -gt 1) {
            Write-Info "[retry] attempt $attempt / $attempts"
            $sp.Write([byte[]]@(0x03), 0, 1)
            [void](Sync-Terminal 1.5)
            Clear-Line
        }

        Start-Sleep -Milliseconds ([int]($Settle * 1000))
        try { $null = $sp.ReadExisting() } catch { }
        [void]$acc.Clear()

        Write-Serial ([System.Text.Encoding]::UTF8.GetBytes($wrapped + "`r")) $wrapped

        $deadline = (Get-Date).AddSeconds($Timeout)
        $tick = 0
        while ((Get-Date) -lt $deadline) {
            try { $chunk = $sp.ReadExisting() } catch { $chunk = "" }
            if ($chunk -and $chunk.Length -gt 0) { [void]$acc.Append($chunk) }
            else { Start-Sleep -Milliseconds 15 }

            $tick++
            if ($tick % 5 -eq 0) {
                if ((Strip-Ansi $acc.ToString()) -match $donePattern) { break }
            }
        }
        Read-For 0.2
        # the board sends its probe right after the command; answer it so the
        # prompt comes back and the next call starts clean
        [void](Sync-Terminal 0.4)

        $clean = Normalize-Lines (Strip-Ansi $acc.ToString())
        $lastRaw = $clean
        $lines = $clean -split "`n"

        $startIdx = -1
        $endIdx = -1
        $rc = $null
        for ($i = 0; $i -lt $lines.Count; $i++) {
            $l = $lines[$i].Trim()
            if ($l -eq $beginTag) { $startIdx = $i }
            if ($l -match ('^' + [regex]::Escape($endTag) + ' rc=(\d+)$')) {
                $endIdx = $i
                $rc = [int]$Matches[1]
            }
        }

        $body = $null
        if ($startIdx -ge 0 -and $endIdx -gt $startIdx) {
            if ($endIdx - $startIdx -le 1) {
                $body = ""
            }
            else {
                $body = ($lines[($startIdx + 1)..($endIdx - 1)] -join "`n")
                $body = $body.Trim([char[]]@("`r", "`n"))
            }
        }

        if ($null -ne $body) {
            if ($body -ne "") { [Console]::Out.WriteLine($body) }
            Write-Info "[rc] $rc"
            if ($null -ne $rc) { exit ([int]$rc) } else { exit 0 }
        }
    }

    Write-Info "[warn] end marker not seen after $attempts attempt(s), raw output follows:"
    [Console]::Out.WriteLine($lastRaw)
    Write-Info "[fail] timeout"
    exit 3
}
finally {
    if ($sp.IsOpen) { $sp.Close() }
    $sp.Dispose()
    Write-Info "[closed] $Port"
}
