"""Synthetic-only desktop/mobile Money Forward import acceptance journey."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from tools.run_ux_e2e import ROOT, BASE_URL, assert_port_free, browser_choice, wait_for_server, route
from tests.test_moneyforward_sync import sample


def main():
    assert_port_free()
    with tempfile.TemporaryDirectory(prefix="personal-os-mf-ux-") as directory:
        root = Path(directory)
        environment = dict(os.environ, PERSONAL_OS_ENV="verification", PERSONAL_OS_PORT="8877",
                           PERSONAL_OS_HOST="127.0.0.1", PERSONAL_OS_DB_PATH=str(root / "ux-synthetic.db"),
                           PERSONAL_OS_ATTACHMENT_DIR=str(root / "attachments"),
                           PERSONAL_OS_BACKUP_DIR=str(root / "backups"), PERSONAL_OS_MONEYFORWARD_DB_PATH="",
                           PERSONAL_OS_RUN_STARTUP_MAINTENANCE="false", PERSONAL_OS_QUEUE_ANALYSIS_ON_START="false")
        subprocess.run([sys.executable, str(ROOT / "tools" / "seed_ux_demo.py"), "--db", str(root / "ux-synthetic.db")],
                       cwd=ROOT, env=environment, check=True, stdout=subprocess.DEVNULL)
        process = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for_server(process)
            with sync_playwright() as playwright:
                executable, browser_kind = browser_choice()
                browser = playwright.chromium.launch(headless=True, **({"executable_path": str(executable)} if executable else {}))
                errors = []
                try:
                    for width, height in ((1280, 720), (390, 844)):
                        context = browser.new_context(viewport={"width": width, "height": height},
                                                      is_mobile=width < 500, has_touch=width < 500)
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(BASE_URL)
                        route(page, "money")
                        section = page.locator("#money-content .domain-integration")
                        expect(section).to_be_visible()
                        section.get_by_text("マネーフォワード情報を更新", exact=True).click()
                        upload = page.locator("#moneyforward-update-file")
                        upload.set_input_files({"name": "invalid.json", "mimeType": "application/json", "buffer": b"{}"})
                        with page.expect_response(lambda response: response.url.endswith("/moneyforward/import") and response.request.method == "POST") as rejected:
                            page.get_by_role("button", name="更新データを取り込む", exact=True).click()
                        assert rejected.value.status == 400
                        expect(page.locator("#moneyforward-import-notice")).not_to_contain_text("反映しました")
                        expect(page.get_by_role("button", name="更新データを取り込む", exact=True)).to_be_enabled()
                        upload.set_input_files({"name": "synthetic.json", "mimeType": "application/json",
                                                "buffer": json.dumps(sample()).encode()})
                        with page.expect_response(lambda response: response.url.endswith("/moneyforward/import") and response.request.method == "POST") as imported:
                            page.get_by_role("button", name="更新データを取り込む", exact=True).click()
                        assert imported.value.status == 201
                        expect(page.locator("#moneyforward-import-notice")).to_contain_text("反映")
                        expect(section).to_contain_text("2026-07-31")
                        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                        page.reload()
                        route(page, "money")
                        expect(page.locator("#money-content .domain-integration")).to_contain_text("2026-07-31")
                        response = context.request.get(BASE_URL + "/api/integrations/moneyforward").json()
                        assert response["source_kind"] == "connector_snapshot"
                        assert response["net_assets"] == 100000
                        context.close()
                    assert not errors, errors
                    print(json.dumps({"status": "PASS", "browser": browser_kind, "viewports": ["1280x720", "390x844"],
                                      "api_import": True, "reload_persistence": True, "error_retry": True,
                                      "horizontal_overflow": False, "console_errors": len(errors)}))
                finally:
                    browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__": main()
