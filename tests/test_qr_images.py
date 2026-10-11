"""The QR upload must select the login code, independent of the page's other QRs."""
import io
import qrcode
from PIL import Image

QUARK = 'https://su.quark.cn/4_eMHBJ?client_id=532&token=TEST_LOGIN_TOKEN_12345&ssb=weblogin'
OTHER = 'https://example.test/download-app'


def screenshot(*raws):
    codes = [qrcode.make(raw, box_size=6, border=4).convert('RGB') for raw in raws]
    canvas = Image.new('RGB', (sum(code.width for code in codes) + 80 * (len(codes) + 1),
                               max(code.height for code in codes) + 160), 'white')
    x = 80
    for code in codes:
        canvas.paste(code, (x, 80))
        x += code.width + 80
    output = io.BytesIO()
    canvas.save(output, format='PNG')
    return output.getvalue()


def upload(client, content, purpose='quark_web'):
    return client.post('/api/qr/decode-image', data={'purpose': purpose},
                       files={'image': ('screenshot.png', content, 'image/png')})


def test_full_screenshot_selects_login_qr_instead_of_download_qr(system):
    _, client, _ = system
    response = upload(client, screenshot(OTHER, QUARK))
    assert response.status_code == 200, response.text
    assert response.json()['raw'] == QUARK
    assert response.json()['provider'] == 'quark_reference'


def test_two_different_login_qrs_are_ambiguous(system):
    _, client, _ = system
    second = QUARK.replace('12345', '67890')
    response = upload(client, screenshot(QUARK, second))
    assert response.status_code == 422
    assert response.json()['detail'] == 'multiple_quark_qrs'
    assert 'TEST_LOGIN_TOKEN' not in response.text


def test_repeated_same_login_qr_is_unambiguous(system):
    _, client, _ = system
    response = upload(client, screenshot(QUARK, QUARK))
    assert response.status_code == 200
    assert response.json()['raw'] == QUARK


def test_general_upload_does_not_choose_one_of_multiple_codes(system):
    _, client, _ = system
    response = upload(client, screenshot(OTHER, QUARK), purpose='general')
    assert response.status_code == 422
    assert response.json()['detail'] == 'multiple_qr_codes'


def test_quark_upload_rejects_non_login_and_fragment_qrs(system):
    _, client, _ = system
    for raw in (OTHER, QUARK + '#other'):
        response = upload(client, screenshot(raw))
        assert response.status_code == 422
        assert response.json()['detail'] == 'invalid_quark_reference_qr'
    assert client.post('/api/qr/parse', json={'raw': QUARK + '#other'}).status_code == 422


def test_transparent_background_is_composited_before_decode(system):
    _, client, _ = system
    qr = qrcode.make(QUARK, box_size=6, border=4).convert('RGBA')
    qr.putdata([(0, 0, 0, 255) if pixel[0] == 0 else (0, 0, 0, 0) for pixel in qr.get_flattened_data()])
    output = io.BytesIO()
    qr.save(output, format='PNG')
    response = upload(client, output.getvalue())
    assert response.status_code == 200, response.text
    assert response.json()['raw'] == QUARK
