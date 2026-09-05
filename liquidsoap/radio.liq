settings.log.stdout.set(true)
settings.log.level.set(3)
settings.server.telnet.set(true)
settings.server.telnet.bind_addr.set("0.0.0.0")
settings.server.telnet.port.set(1234)

queue = request.queue(id="queue")
silence = blank(duration=5.)
radio = fallback(track_sensitive=false, [queue, silence])

# Every listener costs this much upload, so it's tunable: 320 sounds close enough to
# the source for a small private room, but drop it if the connection struggles.
bitrate = int_of_string(environment.get(default="320", "STREAM_BITRATE"))
log.important("Streaming at #{bitrate} kbps")

icecast_port = int_of_string(environment.get(default="6491", "ICECAST_PORT"))

output.icecast(
  %mp3(bitrate=bitrate),
  host="icecast",
  port=icecast_port,
  password=environment.get("ICECAST_SOURCE_PASSWORD"),
  mount="radio.mp3",
  name="tsdfm",
  description="Self-hosted friends radio",
  radio
)
