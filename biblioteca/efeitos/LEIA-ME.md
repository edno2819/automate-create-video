# Efeitos sonoros

Os sons curtos que o diretor visual põe em momentos do vídeo (o impacto da virada, o whoosh da troca de cena, a
batida de coração na tensão). Cadastre cada efeito em **Configurações > Efeitos sonoros**, com um nome que diga o som
("Impacto grave", "Whoosh rápido") e o arquivo de áudio (mp3, m4a, aac, wav, ogg, opus ou flac).

A interface grava o áudio nesta pasta e o índice em `efeitos.json` (id, nome, arquivo e duração de cada um). Um
arquivo posto aqui à mão, sem entrada no índice, não é usado. O id sai do nome no cadastro e nunca muda: as cenas
(`08-cenas/*.json`) guardam o id, e renomear um efeito muda só o nome.

Na montagem, cada efeito toca no corte para a cena ou numa palavra da narração, com o pico levado a -8 dBFS e o
volume pedido pela cena, por cima da voz e da música. Use sons com licença para uso comercial: a Biblioteca de Áudio
do YouTube Studio tem efeitos.
