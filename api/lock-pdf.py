from http.server import BaseHTTPRequestHandler
from io import BytesIO
import cgi

from pypdf import PdfReader, PdfWriter


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )

            if "file" not in form or "password" not in form:
                self._text_response(400, "PDF file and password are required.")
                return

            password = form.getvalue("password", "").strip()
            file_item = form["file"]
            if not password:
                self._text_response(400, "Password cannot be empty.")
                return

            input_bytes = file_item.file.read()
            if not input_bytes:
                self._text_response(400, "Uploaded PDF is empty.")
                return

            reader = PdfReader(BytesIO(input_bytes))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    self._text_response(400, "Encrypted PDFs must be unlocked before re-locking.")
                    return

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)

            if reader.metadata:
                writer.add_metadata({key: str(value) for key, value in reader.metadata.items() if value})

            writer.encrypt(user_password=password, owner_password=password, use_128bit=True)
            output = BytesIO()
            writer.write(output)

            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="yashipdf_secured_locked.pdf"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(output.getvalue())
        except Exception as exc:
            self._text_response(500, f"Unable to lock PDF: {exc}")

    def _text_response(self, status, message):
        data = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
