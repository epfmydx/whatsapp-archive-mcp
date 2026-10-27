from datetime import datetime, timezone
from test_auth import BASE, login


def test_profile_health_starts_unknown_and_does_not_invent_dates(client):
    login(client)
    response = client.get('/profiles/personal', base_url=BASE)
    assert response.status_code == 200
    assert b'Archive health' in response.data and b'Not yet reported' in response.data
    assert b'Not yet recorded' in response.data


def test_profile_health_displays_malaysia_time_for_quiet_connected_account(client, application, monkeypatch):
    epoch = int(datetime(2026, 10, 26, 16, 21, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr('app.health.now', lambda: epoch)
    headers = {'Authorization':'Bearer '+'i'*32}
    message = {'account_id':'personal','message_id':'ui-synthetic','chat_jid':'chat','sender_jid':'sender',
               'timestamp':'2026-10-25T03:01:00Z','text':'Synthetic'}
    assert client.post('/internal/messages',base_url=BASE,headers=headers,json=message).status_code == 200
    assert client.post('/internal/accounts/status',base_url=BASE,headers=headers,json={
        'account_id':'personal','status':'connected','whatsapp_connected':True,
        'whatsapp_authenticated':True,'pending_count':0}).status_code == 200
    login(client)
    response = client.get('/profiles/personal',base_url=BASE)
    assert b'Connected; no pending archive work' in response.data
    assert b'27 Oct 2026, 00:21:00 MYT' in response.data
    assert b'25 Oct 2026, 11:01:00 MYT' in response.data
    assert b'not complete WhatsApp history' in response.data
    monkeypatch.setattr('app.health.now',lambda:epoch+91)
    assert b'Bridge check-in overdue' in client.get('/profiles/personal',base_url=BASE).data


def test_malaysia_time_filter_handles_missing_and_invalid_values(application):
    render = application.jinja_env.filters['malaysia_time']
    assert render(None) == 'Not yet recorded'
    assert render('bad') == 'Unknown'
    assert render('2026-10-27T00:00:00') == 'Unknown'
