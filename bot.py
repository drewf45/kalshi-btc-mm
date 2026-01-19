def _sign(self, method: str, path: str, ts: int, body: str) -> str:
    # IMPORTANT: do NOT include body in the payload
    payload = f"{ts}{method.upper()}{path}".encode("utf-8")

    sig = self.private_key.sign(
        payload,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")