def request_json(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = API_BASE + API_PREFIX + path
    params = params or {}

    # Build exact bytes we will send
    if json_body is None:
        body_bytes = b""
    else:
        body_str = json.dumps(json_body, separators=(",", ":"), sort_keys=True)
        body_bytes = body_str.encode("utf-8")

    session = requests.Session()
    backoff = BACKOFF_START

    while True:
        # Prepare a fresh request each attempt (timestamp/signature must be fresh)
        req = requests.Request(
            method.upper(),
            url,
            params=params,
            data=body_bytes,
        )
        prepped = req.prepare()

        signed_path = prepped.path_url

        # Sign EXACT body string that corresponds to body_bytes
        body_for_sig = "" if not body_bytes else body_bytes.decode("utf-8")

        headers = build_signature_headers