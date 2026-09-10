# ── main.py ───────────────────────────────────────────────────────────────────
import sys
import os

from src.menu.home_controller  import HomeController
from src.menu.home_view        import HomeView
from src.menu.login_controller import LoginController
from src.menu.login_view       import LoginView
import src.menu.authentication.encrypt as encrypt

def main() -> None:
    skip_login = "--no-login" in sys.argv

    home_ctrl = HomeController()
    home_view = HomeView(controller=home_ctrl)
    login_ctrl = LoginController()

    def show_login() -> None:
        login = LoginView(
            master=home_view,
            on_success=on_login_success,
            controller=login_ctrl,
        )
        home_view.wait_window(login)

    def on_login_success(username: str) -> None:
        home_ctrl.set_user(username)
        home_view.show_after_login()

    def clear_user_info():
        current_dir = os.path.dirname(os.path.abspath(__file__))
        info_path = os.path.join(current_dir, "user_info.enc")
        encrypt.delete_encrypted_file(info_path)

    # Re-show login whenever the user signs out
    home_view.bind("<<SignOut>>", lambda _e: clear_user_info())
    home_view.bind("<<SignOut>>", lambda _e: show_login(), add="+")
    current_dir = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(current_dir, "secret.key")
    info_path = os.path.join(current_dir, "user_info.enc")

    if skip_login and os.path.isfile(info_path):
            key = encrypt.load_key(key_path)
            loaded_username = encrypt.read_encrypted_file(info_path, key)
            home_ctrl.set_user(loaded_username)
            home_view.show_after_login()
    else:
        encrypt.generate_and_save_key(key_path)
        show_login()
    home_view.mainloop()


if __name__ == "__main__":
    main()