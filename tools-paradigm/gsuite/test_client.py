from gsuite import client


class _CreateRequest:
    def __init__(self, result: dict):
        self._result = result

    def execute(self) -> dict:
        return self._result


class _FakeFilesApi:
    def __init__(self):
        self.create_calls: list[dict] = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if kwargs["body"].get("mimeType") == "application/vnd.google-apps.folder":
            return _CreateRequest(
                {
                    "id": "folder-123",
                    "name": kwargs["body"]["name"],
                    "webViewLink": "https://drive.google.com/folder/folder-123",
                    "parents": kwargs["body"].get("parents", []),
                }
            )

        return _CreateRequest(
            {
                "id": "file-123",
                "name": kwargs["body"]["name"],
                "webViewLink": "https://drive.google.com/file/file-123",
            }
        )


class _FakeDriveService:
    def __init__(self):
        self.files_api = _FakeFilesApi()

    def files(self):
        return self.files_api


def test_drive_upload_sets_supports_all_drives(tmp_path, monkeypatch):
    upload_file = tmp_path / "example.txt"
    upload_file.write_text("hello")
    fake_service = _FakeDriveService()

    monkeypatch.setattr(client, "get_drive_service", lambda: fake_service)
    monkeypatch.setattr(
        client,
        "MediaFileUpload",
        lambda file_path, mimetype, resumable: {
            "file_path": file_path,
            "mimetype": mimetype,
            "resumable": resumable,
        },
    )

    result = client.drive_upload(str(upload_file), folder_id="parent-123")

    create_call = fake_service.files_api.create_calls[0]
    assert create_call["supportsAllDrives"] is True
    assert create_call["body"]["parents"] == ["parent-123"]
    assert result["id"] == "file-123"
    assert result["name"] == "example.txt"


def test_drive_create_folder_uses_folder_mime_type(monkeypatch):
    fake_service = _FakeDriveService()
    monkeypatch.setattr(client, "get_drive_service", lambda: fake_service)

    result = client.drive_create_folder("Closing Docs", parent_id="parent-123")

    create_call = fake_service.files_api.create_calls[0]
    assert create_call["supportsAllDrives"] is True
    assert create_call["body"] == {
        "name": "Closing Docs",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["parent-123"],
    }
    assert result == {
        "id": "folder-123",
        "name": "Closing Docs",
        "web_view_link": "https://drive.google.com/folder/folder-123",
        "parent_ids": ["parent-123"],
    }
