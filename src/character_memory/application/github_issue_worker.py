"""One disposable HTTP worker; credentials enter via stdin, never argv/files."""
import json
import sys

def main():
    import httpx
    from character_memory.application.collaboration_runtime import GitHubIssueHTTPTransport
    try:
        payload = json.loads(sys.stdin.read(100000))
        token = payload.pop('token')
        writer = GitHubIssueHTTPTransport(token, transport=httpx.HTTPTransport(retries=0))
        result = writer(**payload, idempotency_key='reserved-by-parent')
        sys.stdout.write(json.dumps(result))
        return 0
    except Exception:
        # No request, credential, URL or external response is logged.
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
