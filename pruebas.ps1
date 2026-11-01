# Banco de pruebas entero: servidor y cliente (pytest) y extensión (node --test), con cobertura.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$t = [Diagnostics.Stopwatch]::StartNew()
python -m pytest -q --cov=eb --cov-report=term-missing tests
if ($LASTEXITCODE) { exit $LASTEXITCODE }
node --test --experimental-test-coverage tests/sw.test.mjs
if ($LASTEXITCODE) { exit $LASTEXITCODE }
"Banco entero en {0:N0} s" -f $t.Elapsed.TotalSeconds
