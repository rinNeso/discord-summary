"""Log the bot's Instagram account in and save its session cookies.

Run through ig-login.sh, never directly. yt-dlp has no Instagram password login,
only cookies, so instaloader does the login and its session is written out as
the Netscape cookies.txt that yt-dlp reads. The password is used once and never
stored; if the session dies, run the script again.

Instagram sometimes answers a new login with a "checkpoint" (confirm it was you)
instead. Approving it in the app and retrying usually works. Failing that, log
in on a browser and paste the `sessionid` cookie (option 2).
"""
import getpass
import http.cookiejar
import os
import shutil
import sys
import tempfile

import instaloader
import yt_dlp

OUT = "/out/cookies.txt"


def password_login(L: instaloader.Instaloader) -> bool:
    user = input("Instagram username: ").strip().lstrip("@")
    pw = getpass.getpass("Password (hidden): ")
    try:
        L.login(user, pw)
    except instaloader.TwoFactorAuthRequiredException:
        try:
            L.two_factor_login(input("2FA code: ").strip())
        except instaloader.InstaloaderException as e:
            print(f"2FA failed: {e}")
            return False
    except instaloader.BadCredentialsException:
        print("Wrong password.")
        return False
    except instaloader.InstaloaderException as e:
        print(f"Instagram refused the login: {e}")
        if "checkpoint" in str(e).lower():
            print("Approve the login in the Instagram app (or the link above), then run this "
                  "again - or use option 2.")
        return False
    return True


def session_login(L: instaloader.Instaloader) -> bool:
    print("In a browser logged in as the bot's account: dev tools > Application/Storage >\n"
          "Cookies > instagram.com, and copy the value of `sessionid`.")
    sid = getpass.getpass("sessionid (hidden): ").strip()
    if not sid:
        return False
    L.load_session("", {"sessionid": sid})
    return True


def save(L: instaloader.Instaloader):
    jar = http.cookiejar.MozillaCookieJar(OUT + ".tmp")
    for c in L.context._session.cookies:     # pylint: disable=protected-access
        if c.domain.endswith("instagram.com") and c.value:
            jar.set_cookie(c)
    os.umask(0o077)
    jar.save(ignore_discard=True, ignore_expires=True)
    os.replace(OUT + ".tmp", OUT)


def test(url: str):
    """Fetch a post the way the bot does, with a scratch copy of the cookies
    (yt-dlp rewrites its cookie file on exit)."""
    with tempfile.TemporaryDirectory() as tmp:
        jar = os.path.join(tmp, "cookies.txt")
        shutil.copyfile(OUT, jar)
        opts = {"quiet": True, "no_warnings": True, "cachedir": False, "cookiefile": jar,
                "ignore_no_formats_error": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            print(f"Test failed: {str(e).removeprefix('ERROR: ')}")
            return
        n = len(info.get("entries") or [info])
        print(f"Test OK: @{info.get('channel') or info.get('uploader')}, {n} item(s).")


def main():
    L = instaloader.Instaloader(quiet=True, max_connection_attempts=1)
    print("1) username + password   2) paste a sessionid cookie from a browser")
    ok = session_login(L) if input("Choice [1]: ").strip() == "2" else password_login(L)
    if not ok:
        sys.exit(1)
    who = None
    try:
        who = L.test_login()
    except instaloader.InstaloaderException:
        pass
    print(f"Logged in as @{who}." if who else
          "Couldn't confirm the login (that check is flaky) - test with a link below.")
    save(L)
    print(f"Saved the session to {OUT}. The bot picks it up on the next link; no restart needed.")
    url = input("Instagram link to test (blank to skip): ").strip()
    if url:
        test(url)


if __name__ == "__main__":
    main()
