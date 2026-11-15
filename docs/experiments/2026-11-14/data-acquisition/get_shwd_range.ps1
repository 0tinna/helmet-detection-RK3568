param([long]$Start, [long]$End, [string]$Target)
$ErrorActionPreference = 'Stop'
$request = [System.Net.HttpWebRequest]::Create('https://drive.usercontent.google.com/download?id=1qWm7rrwvjAWs1slymbrLaCf7Q-wnGLEX&export=download&confirm=t')
$request.Timeout = 60000
$request.ReadWriteTimeout = 60000
$request.AddRange($Start, $End)
$response = $request.GetResponse()
if ([int]$response.StatusCode -ne 206) { throw 'Range request did not return HTTP 206' }
$stream = $response.GetResponseStream()
$file = [System.IO.File]::Create($Target)
try { $stream.CopyTo($file) } finally { $file.Dispose(); $stream.Dispose(); $response.Dispose() }
if ((Get-Item -LiteralPath $Target).Length -ne ($End - $Start + 1)) { throw 'Range byte count mismatch' }
