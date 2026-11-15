#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Check', 'Backup', 'Fetch', 'List', 'Verify', 'Upload', 'Unpack', 'Restore', 'RemoveExport')]
    [string]$Action = 'Check',
    [string]$Config = (Join-Path $PSScriptRoot 'remote.config.json'),
    [string]$Snapshot = 'latest',
    [ValidateSet('system', 'home')][string]$Area = 'home',
    [string]$Target = '',
    [string]$Relative = '',
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$configuration = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$idPattern = '^\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}$'
foreach ($key in @('SshTarget', 'Port', 'IdentityFile', 'BackupRoot', 'MachineName', 'RemoteHelper', 'ExportRoot', 'RestoreInbox', 'RestoreStagingRoot', 'ReserveGiB')) {
    if ($null -eq $configuration.PSObject.Properties[$key]) { throw "Missing configuration: $key" }
}
if ($configuration.SshTarget -cnotmatch '^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$') { throw 'Use a simple SSH alias or user@host in SshTarget.' }
if ($configuration.MachineName -cnotmatch '^[a-zA-Z0-9][a-zA-Z0-9_-]*$') { throw 'MachineName must be a simple directory name.' }
if ([int]$configuration.Port -lt 1 -or [int]$configuration.Port -gt 65535) { throw 'Invalid SSH port.' }
if ([int]$configuration.ReserveGiB -lt 8) { throw 'ReserveGiB must be at least 8.' }
if (-not [IO.Path]::IsPathRooted($configuration.BackupRoot)) { throw 'BackupRoot must be an absolute Windows path.' }
foreach ($key in @('RemoteHelper', 'ExportRoot', 'RestoreInbox', 'RestoreStagingRoot')) {
    $value = [string]$configuration.$key
    if ($value -cnotmatch '^/[a-zA-Z0-9/_.-]+$' -or $value.Split('/') -contains '..') { throw "Use an absolute Linux path without spaces or shell metacharacters: $key" }
}
$repository = Join-Path $configuration.BackupRoot $configuration.MachineName

function Quote-Remote([string]$Value) {
    if ($Value.Contains("`n") -or $Value.Contains("`r")) { throw 'Remote arguments cannot contain newlines.' }
    return "'" + $Value.Replace("'", "'\''") + "'"
}

function Connection-Options([switch]$Scp) {
    $portFlag = '-p'
    if ($Scp) { $portFlag = '-P' }
    $items = @($portFlag, [string]$configuration.Port, '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3')
    if ($configuration.IdentityFile) { $items += @('-i', [string]$configuration.IdentityFile) }
    return $items
}

function Quote-NativeArgument([string]$Value) {
    # Windows CreateProcess/CRT quoting; preserve quotes and trailing backslashes.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-Native([string]$Program, [string[]]$NativeArguments) {
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = (Get-Command $Program -ErrorAction Stop).Source
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $false
    $process.StartInfo.Arguments = ($NativeArguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
    # Inherit the console directly: interactive SSH/sudo prompts need no newline
    # and must not be captured by PowerShell's line-oriented output pipeline.
    try {
        $null = $process.Start()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "$Program failed (exit $($process.ExitCode)). Incomplete files were retained." }
    } finally {
        $process.Dispose()
    }
}

function Invoke-Remote([string[]]$RemoteArguments, [switch]$Root) {
    $words = @('python3', [string]$configuration.RemoteHelper) + $RemoteArguments
    $options = @(Connection-Options)
    if ($Root) {
        $words = @('sudo') + $words
        $options += '-tt'
    }
    $command = ($words | ForEach-Object { Quote-Remote $_ }) -join ' '
    # SSH carries text only. Binary backup data always travels through SCP.
    Invoke-Native 'ssh.exe' ($options + @('--', $configuration.SshTarget, $command))
}

function Invoke-Scp([string[]]$Paths) {
    $options = @(Connection-Options -Scp)
    Invoke-Native 'scp.exe' ($options + $Paths)
}

function Read-Manifest([string]$Directory) {
    $manifestPath = Join-Path $Directory 'manifest.json'
    $manifestItem = Get-Item -LiteralPath $manifestPath
    if (($manifestItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Manifest cannot be a reparse point.' }
    $data = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($data.schema -ne 1 -or $data.kind -ne 'portable-linux-backup' -or $data.id -cnotmatch $idPattern) { throw 'Invalid bundle manifest.' }
    $directoryName = Split-Path -Leaf $Directory
    if ($directoryName -cmatch $idPattern -and $directoryName -cne $data.id) { throw 'Bundle directory and manifest ID do not match.' }
    $entries = @($data.files.PSObject.Properties)
    $names = @($entries | ForEach-Object { $_.Name })
    if ($names -cnotcontains 'system.tar.gz' -or $names -cnotcontains 'home.tar.gz') { throw 'Required archive is missing from manifest.' }
    foreach ($entry in $entries) {
        if ($entry.Name -cnotmatch '^[a-z0-9][a-z0-9_.-]{0,63}$' -or $entry.Name -eq 'manifest.json') { throw 'Unsafe filename in manifest.' }
        if ([int64]$entry.Value.bytes -lt 0 -or $entry.Value.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid checksum record.' }
    }
    return $data
}

function Verify-Local([string]$Directory) {
    $data = Read-Manifest $Directory
    foreach ($entry in $data.files.PSObject.Properties) {
        $path = Join-Path $Directory $entry.Name
        $item = Get-Item -LiteralPath $path
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Not a regular file: $path" }
        if ($item.Length -ne [int64]$entry.Value.bytes -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.Value.sha256) {
            throw "Bundle verification failed: $path"
        }
    }
    Write-Host "Verified $($data.id)"
    return $data
}

function Select-Local([string]$Name) {
    if ($Name -eq 'latest') {
        $versions = @(Get-ChildItem -LiteralPath $repository -Directory | Where-Object { $_.Name -cmatch $idPattern } | Sort-Object Name)
        if ($versions.Count -eq 0) { throw 'No completed local bundles.' }
        $Name = $versions[-1].Name
    }
    if ($Name -cnotmatch $idPattern) { throw 'Invalid snapshot ID.' }
    $directory = Join-Path $repository $Name
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) { throw "Bundle not found: $directory" }
    return $directory
}

function Fetch-Bundle([string]$Name) {
    if ($Name -cnotmatch $idPattern) { throw 'Fetch requires an explicit snapshot ID.' }
    $final = Join-Path $repository $Name
    if (Test-Path -LiteralPath $final) {
        $null = Verify-Local $final
        return
    }
    $incoming = Join-Path $repository ('.incoming-' + $Name + '-' + [guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $incoming
    $remoteBase = $configuration.ExportRoot.TrimEnd('/') + '/' + $Name
    Invoke-Scp @("$($configuration.SshTarget):$remoteBase/manifest.json", $incoming)
    $data = Read-Manifest $incoming
    if ($data.id -cne $Name) { throw 'Downloaded manifest does not match requested snapshot.' }
    [int64]$bytes = 0
    foreach ($entry in $data.files.PSObject.Properties) { $bytes += [int64]$entry.Value.bytes }
    $drive = New-Object System.IO.DriveInfo ([IO.Path]::GetPathRoot($incoming))
    if ($drive.AvailableFreeSpace -lt ($bytes + [int64]$configuration.ReserveGiB * 1GB)) { throw 'Insufficient Windows disk space.' }
    $paths = @($data.files.PSObject.Properties | ForEach-Object { "$($configuration.SshTarget):$remoteBase/$($_.Name)" })
    Invoke-Scp ($paths + @($incoming))
    $null = Verify-Local $incoming
    Move-Item -LiteralPath $incoming -Destination $final
    Write-Host "Saved $final"
}

if ($env:OS -ne 'Windows_NT') { throw 'Run this controller in native Windows PowerShell.' }
if ($Action -notin @('List', 'Verify')) {
    $null = Get-Command ssh.exe -ErrorAction Stop
    $null = Get-Command scp.exe -ErrorAction Stop
}
if (-not (Test-Path -LiteralPath $repository)) {
    if ($Action -in @('List', 'Verify', 'Upload', 'Unpack', 'Restore')) { throw 'Backup repository does not exist.' }
    $null = New-Item -ItemType Directory -Path $repository -Force
}
$lock = [IO.File]::Open((Join-Path $repository '.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    switch ($Action) {
        'Check' {
            $null = Invoke-Remote @('plan', '--export-root', $configuration.ExportRoot, '--reserve-gib', [string]$configuration.ReserveGiB) -Root
        }
        'Backup' {
            $name = [DateTime]::UtcNow.ToString("yyyyMMdd'T'HHmmss.ffffff'Z'") + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
            Write-Host "Creating $name"
            Invoke-Remote @('create', '--export-root', $configuration.ExportRoot, '--reserve-gib', [string]$configuration.ReserveGiB, '--snapshot', $name) -Root
            Fetch-Bundle $name
        }
        'Fetch' { Fetch-Bundle $Snapshot }
        'List' {
            Get-ChildItem -LiteralPath $repository -Directory | Where-Object { $_.Name -cmatch $idPattern } | Sort-Object Name | Select-Object Name, FullName
        }
        'Verify' { $null = Verify-Local (Select-Local $Snapshot) }
        'Upload' {
            $directory = Select-Local $Snapshot
            $data = Verify-Local $directory
            $null = Invoke-Remote @('inbox', '--directory', $configuration.RestoreInbox, '--snapshot', $data.id)
            $remote = $configuration.RestoreInbox.TrimEnd('/') + '/' + $data.id
            $paths = @((Join-Path $directory 'manifest.json')) + @($data.files.PSObject.Properties | ForEach-Object { Join-Path $directory $_.Name })
            Invoke-Scp ($paths + @("$($configuration.SshTarget):$remote/"))
            $null = Invoke-Remote @('verify', '--bundle', $remote)
            Write-Host "Uploaded $remote"
        }
        'Unpack' {
            $directory = Select-Local $Snapshot
            $data = Read-Manifest $directory
            $bundle = $configuration.RestoreInbox.TrimEnd('/') + '/' + $data.id
            $destination = $configuration.RestoreStagingRoot.TrimEnd('/') + '/' + $data.id
            $null = Invoke-Remote @('unpack', '--bundle', $bundle, '--destination', $destination, '--reserve-gib', [string]$configuration.ReserveGiB) -Root
        }
        'Restore' {
            if (-not $Target.StartsWith('/')) { throw 'Specify -Target as an absolute Linux directory.' }
            $directory = Select-Local $Snapshot
            $data = Read-Manifest $directory
            $prepared = $configuration.RestoreStagingRoot.TrimEnd('/') + '/' + $data.id
            $remoteArguments = @('restore', '--prepared', $prepared, '--area', $Area, '--target', $Target)
            if ($Relative) { $remoteArguments += @('--relative', $Relative) }
            if ($Apply) { $remoteArguments += '--apply' }
            $null = Invoke-Remote $remoteArguments -Root
        }
        'RemoveExport' {
            $directory = Select-Local $Snapshot
            $data = Verify-Local $directory
            $hash = (Get-FileHash -LiteralPath (Join-Path $directory 'manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
            $null = Invoke-Remote @('remove-export', '--export-root', $configuration.ExportRoot, '--snapshot', $data.id, '--manifest-sha256', $hash) -Root
        }
    }
} finally {
    $lock.Dispose()
}
