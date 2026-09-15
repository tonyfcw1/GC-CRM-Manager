import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from requests.cookies import RequestsCookieJar

from crm_support_ui.app import create_app
from crm_support_ui.forum_gateway import (
    EDIT_URL,
    MAX_ATTACHMENT_COUNT,
    MAX_ATTACHMENT_SIZE,
    ForumAttachment,
    ForumPostError,
    create_forum_post,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00"
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\xff\xd9"


FORM_HTML = """
<html><body>
  <form id="postform" action="forum.php?mod=post&action=newthread&fid=230&topicsubmit=yes">
    <input type="hidden" name="formhash" value="abc123">
    <input type="hidden" name="posttime" value="1700000000">
    <input type="hidden" name="special" value="3">
    <input type="hidden" name="rewardprice" value="1">
    <input type="hidden" name="uid" value="42">
    <input type="hidden" name="hash" value="deadbeefdeadbeefdeadbeefdeadbeef">
    <select name="typeid"><option value="286" selected>未处理</option></select>
    <input name="subject" value="">
    <textarea name="message"></textarea>
    <input name="topicsubmit" value="true">
  </form>
  <script>
    var swfUploadUrl = 'misc.php?mod=swfupload&action=swfupload&operation=upload&fid=230';
  </script>
</body></html>
"""

FORM_HTML_WITH_QUOTED_UPLOAD_CONTEXT = FORM_HTML.replace(
    '    <input type="hidden" name="uid" value="42">\n'
    '    <input type="hidden" name="hash" value="deadbeefdeadbeefdeadbeefdeadbeef">\n',
    "",
).replace(
    "  </form>",
    (
        '  <script>\n'
        '    var uploadConfig = {"uid":"42","hash":"quoted-upload-hash"};\n'
        "  </script>\n"
        "  </form>"
    ),
)


class FakeResponse:
    def __init__(self, body, url, encoding=None):
        self.content = body if isinstance(body, bytes) else body.encode("gbk")
        self.url = url
        self.encoding = encoding

    def raise_for_status(self):
        return None


class AttachmentSession:
    def __init__(
        self,
        upload_response="123",
        upload_responses=None,
        topic_body="<html>发表成功</html>",
        topic_url="https://gcdn.grapecity.com.cn/forum.php?mod=viewthread&tid=123",
        form_html=FORM_HTML,
    ):
        self.headers = {}
        self.cookies = RequestsCookieJar()
        self.upload_response = upload_response
        self.upload_responses = list(upload_responses or [])
        self.topic_body = topic_body
        self.topic_url = topic_url
        self.form_html = form_html
        self.get_calls = []
        self.upload_calls = []
        self.topic_calls = []
        self.cleanup_calls = []
        self.closed = False

    def get(self, url, timeout, headers=None, allow_redirects=True):
        if "deleteattach" in url:
            self.cleanup_calls.append(
                {
                    "url": url,
                    "headers": headers,
                    "timeout": timeout,
                    "allow_redirects": allow_redirects,
                }
            )
            return FakeResponse("1", url)
        self.get_calls.append((url, timeout))
        return FakeResponse(self.form_html, url)

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True, files=None):
        if files:
            upload_response = (
                self.upload_responses.pop(0) if self.upload_responses else self.upload_response
            )
            self.upload_calls.append(
                {
                    "url": url,
                    "data": data,
                    "headers": headers,
                    "files": files,
                    "timeout": timeout,
                }
            )
            return FakeResponse(upload_response, url)
        self.topic_calls.append(
            {
                "url": url,
                "data": data,
                "headers": headers,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        return FakeResponse(
            self.topic_body,
            self.topic_url,
        )

    def close(self):
        self.closed = True


class ForumAttachmentTests(unittest.TestCase):
    def test_extracts_upload_hash_from_quoted_javascript_object_keys(self):
        session = AttachmentSession(form_html=FORM_HTML_WITH_QUOTED_UPLOAD_CONTEXT)
        with patch("crm_support_ui.forum_gateway.requests.Session", return_value=session):
            result = create_forum_post(
                cookie="sid=abc",
                title="主题",
                content="正文",
                attachments=[ForumAttachment("shot.png", PNG_BYTES, "image/png")],
            )

        self.assertEqual(result["attachment_count"], 1)
        upload_query = parse_qs(urlparse(session.upload_calls[0]["url"]).query)
        self.assertEqual(upload_query["uid"], ["42"])
        self.assertEqual(upload_query["hash"], ["quoted-upload-hash"])

    def test_uploads_image_and_binds_it_to_topic(self):
        session = AttachmentSession()
        with patch("crm_support_ui.forum_gateway.requests.Session", return_value=session):
            result = create_forum_post(
                cookie="sid=abc",
                title="中文主题",
                content="正文",
                attachments=[
                    ForumAttachment(
                        filename="截图.png",
                        content=PNG_BYTES,
                        content_type="image/png",
                    )
                ],
            )

        self.assertEqual(result["attachment_count"], 1)
        self.assertEqual(len(session.upload_calls), 1)
        upload = session.upload_calls[0]
        self.assertIn("mod=swfupload", upload["url"])
        upload_query = parse_qs(urlparse(upload["url"]).query)
        self.assertEqual(upload_query["operation"], ["upload"])
        self.assertEqual(upload_query["type"], ["image"])
        self.assertEqual(upload_query["filetype"], ["image/png"])
        self.assertEqual(upload_query["uid"], ["42"])
        self.assertEqual(upload_query["hash"], ["deadbeefdeadbeefdeadbeefdeadbeef"])
        self.assertEqual(upload["data"]["uid"], "42")
        self.assertEqual(upload["data"]["hash"], "deadbeefdeadbeefdeadbeefdeadbeef")
        self.assertEqual(upload["files"]["Filedata"][0], "截图.png")
        self.assertEqual(upload["files"]["Filedata"][1], PNG_BYTES)

        self.assertEqual(len(session.topic_calls), 1)
        topic = session.topic_calls[0]
        fields = parse_qs(
            topic["data"].decode("ascii"), encoding="gbk", keep_blank_values=True
        )
        self.assertEqual(fields["message"], ["正文\n[attachimg]123[/attachimg]"])
        self.assertEqual(fields["attachnew[123][description]"], [""])
        self.assertEqual(fields["attachnew[123][readperm]"], [""])
        self.assertEqual(fields["attachnew[123][price]"], ["0"])
        self.assertTrue(session.closed)

    def test_uploads_multiple_images_in_order(self):
        session = AttachmentSession(upload_responses=["101", "102"])
        with patch("crm_support_ui.forum_gateway.requests.Session", return_value=session):
            result = create_forum_post(
                cookie="sid=abc",
                title="主题",
                content="正文",
                attachments=[
                    ForumAttachment("one.png", PNG_BYTES, "image/png"),
                    ForumAttachment("two.jpg", JPEG_BYTES, "image/jpeg"),
                ],
            )

        self.assertEqual(result["attachment_count"], 2)
        fields = parse_qs(
            session.topic_calls[0]["data"].decode("ascii"),
            encoding="gbk",
            keep_blank_values=True,
        )
        self.assertEqual(fields["message"], ["正文\n[attachimg]101[/attachimg]\n[attachimg]102[/attachimg]"])
        self.assertIn("attachnew[101][price]", fields)
        self.assertIn("attachnew[102][price]", fields)

    def test_accepts_discuz_pipe_delimited_upload_response(self):
        session = AttachmentSession(upload_response="DISCUZUPLOAD|1|0|123|1|forum/x.png|shot.png|0")
        with patch("crm_support_ui.forum_gateway.requests.Session", return_value=session):
            result = create_forum_post(
                cookie="sid=abc",
                title="主题",
                content="正文",
                attachments=[ForumAttachment("shot.png", PNG_BYTES, "image/png")],
            )

        self.assertEqual(result["attachment_count"], 1)
        fields = parse_qs(
            session.topic_calls[0]["data"].decode("ascii"),
            encoding="gbk",
            keep_blank_values=True,
        )
        self.assertEqual(fields["message"], ["正文\n[attachimg]123[/attachimg]"])

    def test_cleans_uploaded_images_when_a_later_upload_fails(self):
        session = AttachmentSession(upload_responses=["101", "not-an-aid"])
        with patch("crm_support_ui.forum_gateway.requests.Session", return_value=session):
            with self.assertRaisesRegex(ForumPostError, "图片编号"):
                create_forum_post(
                    cookie="sid=abc",
                    title="主题",
                    content="正文",
                    attachments=[
                        ForumAttachment("one.png", PNG_BYTES, "image/png"),
                        ForumAttachment("two.png", PNG_BYTES, "image/png"),
                    ],
                )

        self.assertEqual(len(session.topic_calls), 0)
        self.assertEqual(len(session.cleanup_calls), 1)
        cleanup = session.cleanup_calls[0]
        cleanup_query = parse_qs(urlparse(cleanup["url"]).query)
        self.assertEqual(cleanup_query["action"], ["deleteattach"])
        self.assertEqual(cleanup_query["inajax"], ["yes"])
        self.assertEqual(cleanup_query["formhash"], ["abc123"])
        self.assertEqual(cleanup_query["tid"], ["0"])
        self.assertEqual(cleanup_query["pid"], ["0"])
        self.assertEqual(cleanup_query["aids[]"], ["101"])
        self.assertTrue(cleanup["allow_redirects"])

    def test_does_not_delete_images_after_topic_request_has_started(self):
        session = AttachmentSession(topic_body="<html>权限不足</html>", topic_url=EDIT_URL)
        with patch("crm_support_ui.forum_gateway.requests.Session", return_value=session):
            with self.assertRaisesRegex(ForumPostError, "未确认发布成功"):
                create_forum_post(
                    cookie="sid=abc",
                    title="主题",
                    content="正文",
                    attachments=[ForumAttachment("one.png", PNG_BYTES, "image/png")],
                )

        self.assertEqual(len(session.cleanup_calls), 0)

    def test_rejects_an_oversized_image_before_network_call(self):
        with patch("crm_support_ui.forum_gateway.requests.Session") as session_factory:
            with self.assertRaisesRegex(ForumPostError, "不能超过"):
                create_forum_post(
                    cookie="sid=abc",
                    title="主题",
                    content="正文",
                    attachments=[
                        ForumAttachment(
                            "large.png",
                            b"x" * (MAX_ATTACHMENT_SIZE + 1),
                            "image/png",
                        )
                    ],
                )
        session_factory.assert_not_called()

    def test_rejects_too_many_images_before_network_call(self):
        attachments = [
            ForumAttachment(f"{index}.png", b"x", "image/png")
            for index in range(MAX_ATTACHMENT_COUNT + 1)
        ]
        with patch("crm_support_ui.forum_gateway.requests.Session") as session_factory:
            with self.assertRaisesRegex(ForumPostError, "最多"):
                create_forum_post(
                    cookie="sid=abc",
                    title="主题",
                    content="正文",
                    attachments=attachments,
                )
        session_factory.assert_not_called()

    def test_rejects_non_image_before_network_call(self):
        with patch("crm_support_ui.forum_gateway.requests.Session") as session_factory:
            with self.assertRaisesRegex(ForumPostError, "图片"):
                create_forum_post(
                    cookie="sid=abc",
                    title="主题",
                    content="正文",
                    attachments=[
                        ForumAttachment(
                            filename="notes.txt",
                            content=b"not an image",
                            content_type="text/plain",
                        )
                    ],
                )

        session_factory.assert_not_called()

    def test_rejects_bytes_that_only_claim_to_be_an_image(self):
        with patch("crm_support_ui.forum_gateway.requests.Session") as session_factory:
            with self.assertRaisesRegex(ForumPostError, "有效的"):
                create_forum_post(
                    cookie="sid=abc",
                    title="主题",
                    content="正文",
                    attachments=[
                        ForumAttachment(
                            filename="spoof.png",
                            content=b"not really a png",
                            content_type="image/png",
                        )
                    ],
                )

        session_factory.assert_not_called()

    def test_multipart_endpoint_passes_uploaded_file_without_returning_cookie(self):
        session = AttachmentSession()
        with patch(
            "crm_support_ui.app.send_forum_post_with_attachments",
            return_value={
                "url": "https://gcdn.grapecity.com.cn/forum.php?tid=123",
                "title": "主题",
                "attachment_count": 1,
            },
        ) as send:
            with TestClient(create_app(object())) as client:
                response = client.post(
                    "/api/forum-post-with-images",
                    data={"cookie": "sid=secret", "title": "主题", "content": "正文"},
                    files={"attachments": ("shot.png", b"png-bytes", "image/png")},
                )

        self.assertEqual(response.status_code, 201)
        self.assertNotIn("cookie", response.json())
        self.assertEqual(send.call_args.kwargs["cookie"], "sid=secret")
        self.assertEqual(send.call_args.kwargs["title"], "主题")
        self.assertEqual(send.call_args.kwargs["content"], "正文")
        self.assertEqual(len(send.call_args.kwargs["attachments"]), 1)
        attachment = send.call_args.kwargs["attachments"][0]
        self.assertEqual(attachment.filename, "shot.png")
        self.assertEqual(attachment.content, b"png-bytes")
        self.assertEqual(attachment.content_type, "image/png")

    def test_multipart_endpoint_rejects_aggregate_attachment_size(self):
        with patch("crm_support_ui.app.MAX_TOTAL_ATTACHMENT_SIZE", 5):
            with patch("crm_support_ui.app.send_forum_post_with_attachments") as send:
                with TestClient(create_app(object())) as client:
                    response = client.post(
                        "/api/forum-post-with-images",
                        data={"cookie": "sid=secret", "title": "主题", "content": "正文"},
                        files=[
                            ("attachments", ("one.png", b"123", "image/png")),
                            ("attachments", ("two.png", b"456", "image/png")),
                        ],
                    )

        self.assertEqual(response.status_code, 400)
        self.assertIn("总大小", response.json()["detail"])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
