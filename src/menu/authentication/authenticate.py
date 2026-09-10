import requests
import secrets

API_KEY = "AIzaSyDLiNcnTrxcZ6njNb6pLD4jK_GpGbcz6Bg"  # safe to include in the exe

def sign_in(email, password):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={API_KEY}"
    response = requests.post(url, json={
        "email": email,
        "password": password,
        "returnSecureToken": True
    }).json()

    uid = response["localId"]
    
    # Generate a secret ID for this user
    secret_id = secrets.token_hex(32)

    # Store secret_id → uid mapping somewhere only you can see
    # e.g. your own database, Firestore with admin rules, etc.

    return uid, secret_id