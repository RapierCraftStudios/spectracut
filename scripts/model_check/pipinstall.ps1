# Usage: pipinstall.ps1 <venvName> <uv pip install args...>  (e.g. a wheel URL or git+https://...)
param([string]$Name)
if(-not $env:SC_MODELS){ $env:SC_MODELS = Split-Path $PSScriptRoot -Parent }
$env:UV_CACHE_DIR="$env:SC_MODELS\uv-cache"; $env:Path="$env:USERPROFILE\.local\bin;$env:Path"
$ErrorActionPreference="Continue"
uv pip install --python "$env:SC_MODELS\venvs\$Name\Scripts\python.exe" @($args) 2>&1 | Select -Last 6
