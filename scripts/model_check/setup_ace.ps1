# ACE-Step 1.5 (MIT): clone + uv sync into its own venv (torch 2.7.1+cu128 pinned by the repo)
if(-not $env:SC_MODELS){ $env:SC_MODELS = Split-Path $PSScriptRoot -Parent }
$root=$env:SC_MODELS; $env:UV_CACHE_DIR="$root\uv-cache"; $env:HF_HOME="$root\hf"
$env:Path="$env:USERPROFILE\.local\bin;$env:Path"; $ErrorActionPreference="Continue"
if(-not (Test-Path "$root\repos\ace")){ git clone --depth 1 https://github.com/ACE-Step/ACE-Step-1.5.git "$root\repos\ace" 2>&1 | Out-Null }
$py=(Get-ChildItem "$root\python\cpython-3.11.*\python.exe" | Select -First 1).FullName
$env:UV_PROJECT_ENVIRONMENT="$root\venvs\ace"
Set-Location "$root\repos\ace"
uv sync --python $py 2>&1 | Select -Last 8
