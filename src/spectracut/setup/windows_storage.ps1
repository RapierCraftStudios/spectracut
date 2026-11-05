# SPDX-License-Identifier: AGPL-3.0-or-later
# One-time admin script that makes this Windows PC the SpectraCut Flamenco storage host.
# `spectracut setup --storage-host <host>` writes it next to a password file only the SSH
# account and Administrators can read; run it once from an elevated PowerShell, then re-run setup.
# Idempotent. Creates a dedicated hidden account, the SMB share on -Path (setup passes its
# --storage-path) and a Tailscale-only firewall rule. Setup runs it itself when the SSH
# account is an Administrator.
#Requires -RunAsAdministrator
param(
  [Parameter(Mandatory)][string]$PasswordFile,
  [string]$User = 'spectracut-share',
  [string]$Share = 'flamenco',
  [string]$Path = 'C:/spectracut/storage'
)
$ErrorActionPreference = 'Stop'
# A share with this name on another folder is reported, never silently reused; on the
# same folder (setup's --storage-path) it is adopted.
function Get-NormPath([string]$p) { ($p -replace '\\', '/').TrimEnd('/').ToLowerInvariant() }
$existing = Get-SmbShare -Name $Share -ErrorAction SilentlyContinue
if ($existing -and ((Get-NormPath $existing.Path) -ne (Get-NormPath $Path))) {
  throw ("SMB share '$Share' already exists at '$($existing.Path)', not at '$Path'. " +
    "Re-run spectracut setup with --storage-path '$($existing.Path)' to adopt it, or remove the share.")
}
$sec = ConvertTo-SecureString ((Get-Content $PasswordFile -Raw).Trim()) -AsPlainText -Force
if (Get-LocalUser -Name $User -ErrorAction SilentlyContinue) {
  Set-LocalUser -Name $User -Password $sec
} else {
  New-LocalUser -Name $User -Password $sec -PasswordNeverExpires | Out-Null
}
# Hide the account from the logon screen.
reg add 'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList' /v $User /t REG_DWORD /d 0 /f | Out-Null
New-Item -ItemType Directory -Force $Path | Out-Null
icacls ($Path -replace '/', '\') /grant ($User + ':(OI)(CI)M') | Out-Null
if ($existing) {
  Grant-SmbShareAccess -Name $Share -AccountName $User -AccessRight Full -Force | Out-Null
} else {
  New-SmbShare -Name $Share -Path $Path -FullAccess $User | Out-Null
}
if (-not (Get-NetFirewallRule -Name spectracut-smb -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name spectracut-smb -DisplayName 'SpectraCut storage (Tailscale only)' `
    -Direction Inbound -Protocol TCP -LocalPort 445 -RemoteAddress 100.64.0.0/10 -Action Allow | Out-Null
}
# Marker setup checks for, in the folder setup asked for (-Path).
New-Item -ItemType File -Force (Join-Path $Path '.spectracut-share') | Out-Null
Remove-Item $PasswordFile -Force
Write-Host "Storage share \\$env:COMPUTERNAME\$Share is ready; re-run `spectracut setup`."
