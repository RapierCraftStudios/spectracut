# Usage: setup_repo.ps1 -Name foley -Url https://github.com/... -Dir foley [-Req requirements.txt]
# Clones a repo under $SC_MODELS\repos\<Dir> and prints its requirements (no auto-install: pins often clash with torch).
param([string]$Name,[string]$Url,[string]$Dir,[string]$Req="requirements.txt")
if(-not $env:SC_MODELS){ $env:SC_MODELS = Split-Path $PSScriptRoot -Parent }
$ErrorActionPreference="Continue"
$d="$env:SC_MODELS\repos\$Dir"
if(-not (Test-Path $d)){ git clone --depth 1 $Url $d 2>&1 | Out-Null }
Get-ChildItem $d | select -First 40 Name | Out-String
Get-Content "$d\$Req" -ErrorAction SilentlyContinue
