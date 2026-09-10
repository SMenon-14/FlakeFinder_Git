from cryptography.fernet import Fernet
import os 

def generate_and_save_key(key_path="secret.key"):
    """Generates a secure key and saves it to a file."""
    key = Fernet.generate_key()
    with open(key_path, "wb") as key_file:
        key_file.write(key)

def load_key(key_path="secret.key"):
    """Loads the encryption key from the specified file."""
    with open(key_path, "rb") as key_file:
        return key_file.read()

def write_encrypted_file(file_path, plain_text, key):
    """Encrypts a string and writes it to a file."""
    fernet = Fernet(key)
    # Data must be encoded to bytes before encryption
    secret_bytes = plain_text.encode('utf-8')
    encrypted_data = fernet.encrypt(secret_bytes)
    
    with open(file_path, "wb") as enc_file:
        enc_file.write(encrypted_data)

def read_encrypted_file(file_path, key):
    """Reads an encrypted file and decrypts it back to plain text."""
    fernet = Fernet(key)
    
    with open(file_path, "rb") as enc_file:
        encrypted_data = enc_file.read()
        
    decrypted_bytes = fernet.decrypt(encrypted_data)
    # Decode bytes back into a standard Python string
    return decrypted_bytes.decode('utf-8')

def delete_key_file(key_path="secret.key"):
    if os.path.exists(key_path):
        os.remove(key_path)
        print("Key deleted.")
    else:
        print("Key not found.")

def delete_encrypted_file(file_path):
    if os.path.exists(file_path):
        os.remove(file_path)
        print("Encrypted file deleted.")
    else:
        print("Encrypted file not found.")
