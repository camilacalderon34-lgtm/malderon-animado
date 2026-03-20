# Bugs Fixed

## [2026-03-19] Numbered entries con formato palabra no detectados
- **Problema**: `_RE_NUMBERED` solo matcheaba "Number 5" (digitos), no "Number two" (palabras)
- **Impacto**: Number 1 y Number 2 pierden su etiqueta en Casino (el script usa "Number one", "Number two")
- **Fix**: Expandir regex para incluir one-fifteen en `_RE_NUMBERED`, `_RE_TITLE_END` y bare number detection
- **Archivo**: `app/services/claude_service.py` linea 15
- **Test**: validate.py tests 6d-6h

## [2026-03-19] Merge agresivo destruia limites entre temas
- **Problema**: Bloque DB-level merge en pipeline_service.py fusionaba escenas <=5 palabras sin respetar numbered entries
- **Impacto**: "in perfect harmony." (final Number 8) se pegaba con "Number 9: The film is based on..."
- **Fix**: Eliminar merge agresivo, usar `_merge_short_scenes` con guardas
- **Archivo**: `app/services/pipeline_service.py` (bloque lineas 884-918 eliminado)

## [2026-03-19] _force_split_numbered_titles creaba escenas ultra-cortas
- **Problema**: Splitteaba "Number 10." (2 palabras) como escena independiente de 0.9s
- **Impacto**: Escenas inutiles para TTS y render
- **Fix**: Guarda >6 palabras antes de split en `_force_split_numbered_titles` y post-repair split
- **Archivo**: `app/services/claude_service.py`
- **Test**: validate.py tests 6b-6c

## [2026-03-19] _restore_missing_number_labels usaba Whisper SRT en vez del script original
- **Problema**: `_restore_missing_number_labels()` recibia `full_text` (del Whisper SRT) como referencia para buscar labels faltantes. El Whisper SRT (`subtitles-whisper.srt`) solo contenia Number 10, 7, 6, 5, 4, 3 — faltaban Number 9, 8, 2, 1
- **Impacto**: La funcion solo encontraba 3 labels en vez de 10, no podia restaurar Number 2 ni Number 1 en Casino
- **Fix**: Usar `_script_text` (script original con todos los labels) como referencia en vez de `full_text`
- **Archivo**: `app/services/claude_service.py` linea 597
- **Leccion**: Whisper no siempre transcribe etiquetas de texto correctamente. El script original del TTS es la fuente confiable para labels
