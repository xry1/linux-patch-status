"""Windows HTTPS fallback when Python cannot build a certificate chain.

Windows performs normal certificate validation. Secrets and bodies travel via
stdin, never command arguments; the fixed script does not evaluate input as code.
"""
import json
import subprocess
import urllib.request


class NativeHTTPError(Exception):
    def __init__(self, status=0):
        self.status = status
        super().__init__('Native HTTPS request failed')


SCRIPT = r'''
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
try {
    $inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $headers = @{'User-Agent'='LinuxPatchStatus/1.0';'Accept'='application/json'}
    if ($inputData.token) { $headers.Authorization = 'Bearer ' + $inputData.token }
    if (([Uri]$inputData.url).AbsolutePath.EndsWith('/messages')) { $headers['anthropic-version'] = '2023-06-01' }
    $request = @{Uri=$inputData.url;Method=$inputData.method;Headers=$headers;TimeoutSec=45;MaximumRedirection=0;UseBasicParsing=$true}
    if ($inputData.proxy) { $request.Proxy = $inputData.proxy }
    if ($inputData.method -eq 'POST') {
        $request.ContentType = 'application/json; charset=utf-8'
        $request.Body = [Text.Encoding]::UTF8.GetBytes($inputData.body)
    }
    $result = Invoke-WebRequest @request
    if ($result.RawContentLength -gt 1048576) { throw 'response limit' }
    @{ok=$true;status=[int]$result.StatusCode;body=[string]$result.Content} | ConvertTo-Json -Compress
} catch {
    $status = 0
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    @{ok=$false;status=$status} | ConvertTo-Json -Compress
}
'''


def windows_json(url, value=None, token=None):
    request = {'url': url, 'method': 'GET' if value is None else 'POST',
               'body': json.dumps(value, ensure_ascii=False), 'token': token,
               'proxy': urllib.request.getproxies().get('https', '')}
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', SCRIPT],
                                input=json.dumps(request), capture_output=True, text=True, encoding='utf-8',
                                timeout=55, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            raise NativeHTTPError()
        response = json.loads(result.stdout.lstrip('\ufeff'))
        if not response.get('ok'):
            raise NativeHTTPError(response.get('status', 0))
        return json.loads(response['body'])
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        raise NativeHTTPError() from None
