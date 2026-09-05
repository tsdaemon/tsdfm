settings.log.stdout.set(true)
settings.log.level.set(3)
settings.server.telnet.set(true)
settings.server.telnet.bind_addr.set("0.0.0.0")
settings.server.telnet.port.set(1234)

queue = request.queue(id="queue")
silence = blank(duration=5.)
radio = fallback(track_sensitive=false, [queue, silence])

output.icecast(
  %mp3(bitrate=192),
  host="icecast",
  port=8000,
  password=environment.get("ICECAST_SOURCE_PASSWORD"),
  mount="radio.mp3",
  name="tsdfm",
  description="Self-hosted friends radio",
  radio
)
