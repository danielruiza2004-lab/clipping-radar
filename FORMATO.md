# Formato común de los clips (copiado del ejemplo de referencia)

Todos los clips salen igual. Lo aplica automáticamente `scripts/make_clips.py`.

## Imagen — 1080x1920, 30 fps
- **Facecam arriba** (38 % de la altura, sin bordes), recortada de la cámara del streamer.
- **Juego abajo** (62 %), recortado al centro con zoom y sin pisar la facecam.
- Si un canal aún no tiene su facecam configurada (`facecam` en `config.json`),
  se usa el formato provisional: juego en el centro con fondo desenfocado.

## Textos
- **Frase gancho**: blanca, con sombra suave, justo debajo de la facecam, fija
  todo el vídeo (como "Quiero que se repita la ocasión 🗣️"). Sale del título del
  clip más visto de ese momento; si no hay, el clip va sin gancho.
- **Subtítulos** palabra a palabra, en mayúsculas gruesas, blancos con borde
  negro, palabra activa en **amarillo**, máx. 3 palabras, en la mitad baja del juego.
- **@streamer** pequeño en la esquina inferior izquierda de la facecam.

## Duración
- De 15 a 90 s (ajustable en `config.json → clips`). El clip empieza unos
  segundos antes de que el chat reaccione, para coger el contexto.

## Selección
- 3 clips por canal de media; hasta 5 si hay momentos muy fuertes; menos si el día fue flojo.

## Nombres
`AAAA-MM-DD_<canal>_<nº>_<risa|hype>.mp4`, publicados en la Release `clips-AAAA-MM-DD`
y listados en `data/clips-latest.json` (con transcripción, enlace al momento del directo, etc.).
