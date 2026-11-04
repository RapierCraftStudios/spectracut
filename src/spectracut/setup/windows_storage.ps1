# SPDX-License-Identifier: AGPL-3.0-or-later
# One-time admin script that makes this Windows PC the SpectraCut Flamenco storage host.
# `spectracut setup --storage-host <host>` writes it next to a password file only the SSH
# account and Administrators can read; run it once from an elevated PowerShell, then re-run setup.
# Idempotent. Creates a dedicated hidden account, the SMB share and a Tailscale-only firewall rule.
#Requires -RunAsAdministrator
param(
  [Parameter(Mandatory)][string]$PasswordFile,
  [string]$User = 'spectracut-share',
  [string]$Share = 'flamenco',
  [string]$Path = 'C:/spectracut/storage'
)
$ErrorActionPreference = 'Stop'
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
if (-not (Get-SmbShare -Name $Share -ErrorAction SilentlyContinue)) {
  New-SmbShare -Name $Share -Path $Path -FullAccess $User | Out-Null
}
if (-not (Get-NetFirewallRule -Name spectracut-smb -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name spectracut-smb -DisplayName 'SpectraCut storage (Tailscale only)' `
    -Direction Inbound -Protocol TCP -LocalPort 445 -RemoteAddress 100.64.0.0/10 -Action Allow | Out-Null
}
# Marker the non-admin setup run checks for.
New-Item -ItemType File -Force (Join-Path $Path '.spectracut-share') | Out-Null
Remove-Item $PasswordFile -Force
Write-Host "Storage share \\$env:COMPUTERNAME\$Share is ready; re-run `spectracut setup`."
