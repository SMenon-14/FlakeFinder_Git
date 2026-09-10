import requests

# Replace with your actual Firebase Web API Key
# Found in Firebase Console -> Project Settings -> General
API_KEY = "AIzaSyDLiNcnTrxcZ6njNb6pLD4jK_GpGbcz6Bg"

def check_credentials(username, password):
    """
    1. Check credentials (Sign In)
    Returns: (uid, r2_folder, id_token) if successful, None if failed.
    """
    email = f"{username}@flakefinder.com"
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={API_KEY}"
    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True
    }
    
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        data = response.json()
        uid = data["localId"]
        id_token = data["idToken"] # Needed to authorize "get user info"
        r2_folder = f"users/{uid}/"
        return uid, r2_folder, id_token
    else:
        print(f"Sign-in failed: {response.json().get('error', {}).get('message', 'Unknown error')}")
        return None


def get_user_info(id_token):
    """
    2. Get user info
    Requires the id_token retrieved during sign-in or registration.
    """
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={API_KEY}"
    payload = {
        "idToken": id_token
    }
    
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        # Firebase returns a list of users; we grab the first one
        return response.json().get("users", [{}])[0]
    else:
        print(f"Failed to fetch user info: {response.json().get('error', {}).get('message', 'Unknown error')}")
        return None


def register_new_user(username, password):
    """
    3. Register new user
    Returns: (uid, r2_folder, id_token) if successful, None if failed.
    """
    email = f"{username}@flakefinder.com"
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={API_KEY}"
    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True
    }
    
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        data = response.json()
        uid = data["localId"]
        id_token = data["idToken"]
        r2_folder = f"users/{uid}/"
        return uid, r2_folder, id_token
    else:
        print(f"Registration failed: {response.json().get('error', {}).get('message', 'Unknown error')}")
        return None