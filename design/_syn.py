import ast, io
for f in ['host_server.py','vendor/device/channel.py','vendor/device/preset_player.py','vendor/device/quick_moves.py']:
    ast.parse(io.open(f, encoding='utf-8').read())
print('PY OK')
