"""The public edition cannot grant or dispatch MCP write capabilities."""
from sqlalchemy import select
from app.models import IdentityAlias
from app.permissions import READ_SCOPE, normalize_scope
from test_auth import BASE, CHALLENGE, authorize, exchange, login


def rpc(client, token, method, params):
    return client.post('/mcp', base_url=BASE, headers={'Authorization':'Bearer '+token},
                       json={'jsonrpc':'2.0','id':1,'method':method,'params':params})


def test_only_read_tools_are_advertised_and_mutations_cannot_be_called(client, application):
    issued=exchange(client, authorize(client)).json
    assert issued['scope']==READ_SCOPE
    catalog=rpc(client,issued['access_token'],'tools/list',{}).json['result']['tools']
    assert {item['name'] for item in catalog} == {'list_accounts','list_chats','search_contacts','search_messages','get_message','get_media','read_image'}
    assert all(item['annotations']['readOnlyHint'] and not item['annotations']['destructiveHint'] for item in catalog)
    for name in ('set_alias','remove_alias','send_message','send_media','send_reaction','delete_message','mark_read','reply'):
        response=rpc(client,issued['access_token'],'tools/call',{'name':name,'arguments':{'account_id':'personal'}})
        assert response.status_code==200 and response.json['error']['code']==-32602
        assert response.json['error']['message']=='Unknown tool'
    with application.extensions['db_session']() as db:
        assert list(db.scalars(select(IdentityAlias)))==[]


def test_write_scopes_rejected_by_discovery_registration_authorization_and_refresh(client):
    discovery=client.get('/.well-known/oauth-authorization-server',base_url=BASE).json
    assert discovery['scopes_supported']==[READ_SCOPE]
    login(client)
    for scope in ('archive:aliases:write','whatsapp:read archive:aliases:write','whatsapp:send'):
        assert normalize_scope(scope)==''
        assert client.post('/oauth/register',base_url=BASE,json={'redirect_uris':['https://client.test/callback'],'scope':scope}).status_code==400
        assert client.get('/oauth/authorize',base_url=BASE,query_string={'client_id':'desktop','response_type':'code','redirect_uri':'https://client.test/callback','resource':BASE+'/mcp','scope':scope,'code_challenge':CHALLENGE,'code_challenge_method':'S256'}).status_code==400
    issued=exchange(client,authorize(client)).json
    response=client.post('/oauth/token',base_url=BASE,data={'grant_type':'refresh_token','client_id':'desktop','resource':BASE+'/mcp','refresh_token':issued['refresh_token'],'scope':'whatsapp:read archive:aliases:write'})
    assert response.status_code==400
