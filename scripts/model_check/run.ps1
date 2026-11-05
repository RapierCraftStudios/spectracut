# Usage: run.ps1 <venvName> <script.py> [args...]
param([string]$Name,[string]$Script)
if(-not $env:SC_MODELS){ $env:SC_MODELS = Split-Path $PSScriptRoot -Parent }
$env:HF_HOME="$env:SC_MODELS\hf"; $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="$env:SC_MODELS\model_check"
$ErrorActionPreference="Continue"
if($env:COMPUTERNAME -eq "MRDUBEY" -and -not $env:SC_RAM_LIMIT_GB){ $env:SC_RAM_LIMIT_GB="3.8" }  # owner rule: <= 4 GB RAM on this PC
& "$env:SC_MODELS\venvs\$Name\Scripts\python.exe" "$env:SC_MODELS\model_check\$Script" @($args)
