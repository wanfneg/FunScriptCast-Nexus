import ast, io
for f in ['host_server.py','vendor/device/sync_engine.py','vendor/device/preset_player.py']:
    ast.parse(io.open(f, encoding='utf-8').read())
print('PY OK')
