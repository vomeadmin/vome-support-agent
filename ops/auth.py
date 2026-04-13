"""
ops/auth.py

Simple bearer token auth for all /ops/ endpoints.
Token is set via OPS_TOKEN environment variable.
"""

import os

from fastapi import Header, HTTPException


def verify_ops_token(authorization: str = Header(...)):
    expected = os.environ.get("OPS_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="OPS_TOKEN not configured on server",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid token")
