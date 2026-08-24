# Como o dualmaker funciona

O dualmaker cria um MKV com o vídeo de um lançamento e o áudio dublado em português de outro lançamento. O caso mais comum é combinar um arquivo que tem áudio original e português com um arquivo que tem uma versão melhor do vídeo e apenas o áudio original.

Exemplo:

```text
Minions.and.Monsters.2026.1080p.iT.WEB-DL.DUAL-C76.mkv
Minions.and.Monsters.2026.1080p.MA.WEB-DL-BYNDR.mkv
```

O primeiro arquivo é a fonte DUAL. O segundo é o master. O vídeo, os capítulos, as tags principais e o nome final vêm do master. O dualmaker sincroniza as faixas que serão importadas antes de criar o arquivo final. Os arquivos de entrada permanecem intactos.

## A ideia central

O programa trata cada arquivo como uma coleção de faixas, e não como um nome de arquivo com uma extensão. A palavra `DUAL` no nome ajuda na descoberta, mas não decide sozinha o papel do arquivo. A decisão usa principalmente a topologia dos áudios:

- a fonte DUAL precisa ter um áudio de programa em português;
- ela também precisa ter um idioma original não português;
- o master precisa conter uma faixa do idioma original compartilhado;
- faixas de comentário não entram como dublagem de programa.

Em uma operação normal, o master é a linha do tempo de vídeo. O dualmaker calcula como a linha do tempo da fonte DUAL se relaciona com ela e aplica o mesmo mapa às dublagens e às legendas que forem importadas.

## Fluxo completo

Uma execução passa por estas fases:

1. resolve a configuração;
2. verifica dependências, permissões e caminhos;
3. inspeciona os MKVs com MediaInfo, `mkvmerge -J` e `ffprobe`;
4. identifica filmes, séries, temporadas e episódios;
5. encontra pares DUAL e master;
6. escolhe os áudios originais, dublados e as legendas;
7. detecta recaps e outros cortes de abertura quando a opção está ativa;
8. sincroniza os áudios comuns com Milksync;
9. aplica o mapa de sincronização às faixas importadas;
10. trata diferenças de FPS ou de edição quando o modo experimental foi autorizado;
11. monta o novo MKV com `mkvmerge`;
12. verifica o resultado e faz a publicação atômica;
13. grava um relatório JSON.

O trabalho temporário fica em `.dualmaker-work` dentro do caminho configurado. O programa não usa `/tmp` por padrão.

## Descoberta e pareamento

### Filmes

O nome é normalizado para comparar título e ano. Resolução, provedor, HDR, Dolby Vision, codec, áudio, token `DUAL` e grupo de lançamento não precisam ser iguais.

Sem ano no nome, o título normalizado precisa ser exatamente igual e as durações precisam ser compatíveis.

### Séries

O programa extrai o nome da série, a temporada e a identidade completa do episódio. Estes exemplos pertencem ao mesmo episódio:

```text
Furious.S01E01.The.Gorgon.2160p.HULU.WEB-DL-FLUX.mkv
Furious.S01E01.The.Gorgon.1080p.DSNP.WEB-DL.DUAL-RiPER.mkv
```

Também são aceitos nomes com espaços, ano entre parênteses e grupos diferentes:

```text
Warehouse 13 (2009) - S03E01 - The New Guy ... DUAL-JK.mkv
warehouse.13.s03e01.1080p.bluray.x264-shortbrehd.mkv
```

O pareamento usa a série e `SxxExx`, não a resolução ou o provedor. Se houver mais de uma possibilidade com pontuação próxima, o modo automático não escolhe no escuro. Ele registra o caso como ambíguo. O modo interativo permite revisar a escolha.

### Varredura

Por padrão, somente os MKVs diretamente dentro do caminho informado entram na varredura. Use `--recursive` para incluir subpastas. A estrutura relativa é preservada dentro de `dualmaker-output`.

As pastas de saída, de trabalho, ambientes virtuais e arquivos que já terminam em `DUAL-alfaHD` são ignorados. A política evita que uma saída antiga vire entrada de outra execução.

## Inspeção de mídia

O dualmaker coleta o JSON completo do MediaInfo para informações de codec, duração, idioma, canais, bitrate, frequência, flags e nomes. Usa `mkvmerge -J` como autoridade para IDs e flags de Matroska. Usa `ffprobe` para tempos de pacotes, duração de streams e informações de vídeo.

Essa separação importa porque o container pode ter uma duração enganosa. Uma legenda que termina depois do vídeo não deve prolongar artificialmente o episódio. O programa compara o vídeo e os áudios selecionados, e registra divergências no relatório.

## Seleção de faixas

### Áudio

O melhor áudio dublado e o melhor áudio original são escolhidos de forma independente. O original não precisa vir do mesmo arquivo que o dub.

O ranking considera codec, bitrate, canais, frequência de amostragem, duração, completude, idioma, título, flag default, preferência de origem e resultados de análise de sincronização. Por padrão, a fonte DUAL tem preferência para dubs e o master para o original quando a qualidade é equivalente.

A ordem final é:

1. melhor dub português, como faixa default;
2. outros dubs portugueses de programa, sem default;
3. melhor áudio original, sem default.

Faixas de comentário ficam fora da seleção automática. Para controlar a escolha, use IDs de faixa ou seletores de origem:

```console
dualmaker --dual dual.mkv --normal master.mkv \
  --dub-track dual:2 \
  --dub-track dual:3 \
  --original-track master:1
```

`--dual-audio ID` e `--normal-audio ID` continuam disponíveis como atalhos compatíveis. `--preferred-original-source`, `--preferred-dub-source`, `--audio-codec-preference` e `--audio-selection-margin` também podem ser definidos no arquivo de configuração.

Quando duas opções ficam próximas demais, o modo interativo mostra os metadados para escolha manual. No modo automático, o trabalho é pulado e aparece no relatório.

### Legendas internas

O master fornece o conjunto principal. A política padrão `prefer-master` mantém a legenda do master quando já existe uma posição equivalente por idioma, forced ou acessibilidade. A fonte DUAL contribui com posições ausentes.

Use `subtitle_policy: exact-union` ou `--subtitle-policy exact-union` para manter toda legenda que não seja uma duplicata exata segundo conteúdo, tempo, idioma, título e flags.

A ordem final é:

1. português forced, com a preferida como default;
2. português normal, SDH, CC e outras variantes, sem default;
3. inglês, inclusive forced, sem default;
4. demais idiomas em ordem alfabética, sem default.

O dualmaker preserva idioma, título, forced e hearing-impaired. Não promove uma legenda normal a default quando não existe uma legenda portuguesa forced adequada.

### PGS e VobSub

PGS e VobSub são legendas bitmap. O conteúdo visual não é convertido. O programa ajusta os tempos dos display sets ou dos pacotes conforme o mapa de cortes e atrasos.

Se todos os pacotes precisarem do mesmo atraso, VobSub pode ser copiado diretamente. Se a faixa exigir vários atrasos por causa de cortes diferentes, o dualmaker procura a substituição equivalente do master. Com `prefer-master`, uma faixa bitmap problemática pode ser omitida quando o master já oferece a mesma posição. Caso contrário, o trabalho para com o motivo no relatório, porque aplicar um único atraso produziria uma legenda incorreta.

### Legendas externas

São reconhecidos `.srt`, `.ass`, `.ssa` e `.sub` textual associados ao nome do MKV. Uma legenda ao lado da fonte DUAL recebe `pt-BR` por padrão, o que permite processar temporadas sem perguntas. Legendas ao lado do master exigem escolha interativa ou mapeamento explícito.

```console
dualmaker /media/temporada --sidecar-language \
  'Warehouse 13 ... DUAL-JK.srt=en'
```

No arquivo de configuração, use `sidecar_dual_language` e `sidecar_language_overrides`. Antes da leitura, todo sidecar é convertido para UTF-8 com BOM. Entradas UTF-8, Windows-1252 e Latin-1 são aceitas sem alterar o arquivo original.

## Sincronização com Milksync

O ponto de referência é o áudio original compartilhado entre os dois arquivos. O dualmaker não compara o português com o inglês para decidir o atraso. Ele envia ao Milksync os dois originais correspondentes, geralmente na forma:

```text
fonte DUAL: áudio original
master:     áudio original
```

O Milksync encontra pontos de sincronização e cria um mapa por trechos. Esse mapa pode ter atrasos diferentes ao longo do episódio. Cada trecho informa a posição na fonte, a posição no master e o deslocamento correspondente.

Antes da sincronização, o programa também observa o primeiro timestamp dos pacotes. A diferença entre os timestamps dos dois áudios originais fica registrada como diagnóstico, mas não é aplicada automaticamente ao dub: ela pode ser um erro de mux do áudio do master ou PTS de priming do codec e não prova um deslocamento em relação ao vídeo do master. A renderização usa somente o mapa acústico na linha do tempo do vídeo master; assim, um PTS de áudio diferente em uma release não cria um atraso fixo em todos os episódios.

O mesmo mapa é usado para:

- todos os dubs portugueses selecionados;
- legendas textuais da fonte DUAL;
- PGS e VobSub quando a estratégia de timestamps permite;
- ajustes de linha do tempo aplicados ao áudio original.

Os controles avançados `--align-framerate`, `--align-frames-too`, `--only-delta`,
`--adjust-delay` e `--preserve-silence` ficam separados do fluxo padrão. Eles são úteis para
diagnóstico ou para uma fonte conhecida, mas não substituem a validação automática. `--adjust-delay`
é um override explícito da linha do tempo; ele não desativa a comparação dos áudios originais.

O vídeo do master nunca é reencodado. O áudio é copiado quando os cortes permitem. Se uma operação exige inserir áudio original, silêncio ou unir trechos incompatíveis, o dualmaker reencoda somente a faixa afetada, normalmente para FLAC, e registra isso como fallback de codec. Mesmo em um mapa TVRip acústico aceito, o programa verifica a colocação constante contra o vídeo master quando encontra âncoras visuais confiáveis.

### Recap de abertura

Com `trim_recap` ativo, o programa procura nos primeiros 120 segundos por trechos pretos, keyframes seguros e evidência de áudio comum depois do corte. Ele só remove uma abertura quando um candidato é claramente melhor. Se a análise não for conclusiva, o trecho é mantido ou enviado para revisão interativa.

### Falha parcial do dub

Uma dublagem pode não conter uma cena que existe no master. Isso pode acontecer no meio do episódio, e não apenas no fim. O dualmaker identifica lacunas da linha do tempo do master e, quando a cobertura e a confiança permitem, usa o áudio original do master naquele intervalo. O restante do dub continua sendo usado.

O comportamento é controlado por `dub_gap_fallback`:

- `original`: preenche com o original do master;
- `silence`: mantém a duração com silêncio;
- `off`: não aplica o reparo;
- `ask`: pede decisão quando a situação exige revisão.

O relatório lista os intervalos exatos usados no fallback. O nome da faixa permanece curto, por exemplo `Portuguese (Brazil)`. Os detalhes de cobertura ficam no JSON, não no título do MKV.

## Diferença de FPS

FPS diferente não significa automaticamente que é preciso acelerar ou desacelerar o áudio. A diferença pode vir de telecine, duplicação de frames, conversão de cadence ou de uma mudança real na velocidade do programa.

O dualmaker lê as taxas como frações exatas, por exemplo `24000/1001` e `30000/1001`. Mostra o drift nominal antes de continuar. O modo normal exige taxas iguais. Para experimentar com taxas compatíveis, use:

```console
dualmaker /media/releases \
  --allow-experimental-fps-sync \
  --allow-tvrip-segment-sync
```

O fluxo experimental tenta, nesta ordem, relações de tempo e conteúdo que podem ser comprovadas. Ele usa âncoras de áudio, janelas espectrais e, quando disponível, um mapa segmentado de vídeo. Não altera a velocidade só porque o container informa FPS diferente.

Em uma conversão 29,97 para 23,976, o modo pode reconhecer uma relação de telecine. Nesse caso, o áudio continua em tempo real e a validação passa para o mapa acústico e para os segmentos. O programa aceita uma confirmação de vídeo pós-sincronização quando a prova espectral antes e depois é confiável. Um conjunto fraco de âncoras visuais ainda causa rejeição.

## TVRip e fontes com cortes editoriais

TVRip é um fluxo experimental separado. Ele atende casos com comerciais, chamadas de emissora, recaps alternativos, censura, abertura encurtada ou créditos diferentes.

Use `--tvrip` para declarar a origem explicitamente. Sem `--allow-tvrip-segment-sync`, o modo automático não processa a fonte. O vídeo do master continua sendo imutável.

O fluxo:

1. usa os áudios originais comuns para gerar o mapa de trechos;
2. limita cada trecho às durações reais de fonte e master;
3. divide trechos longos em fatias de validação;
4. compara o conteúdo no início, meio e fim das fatias;
5. classifica material somente da TVRip como intervalo de fonte;
6. cria intervalos somente do master quando um segmento é rejeitado;
7. aplica o fallback escolhido à dublagem.

Quando o modo telecine acústico é aceito, cada bucket de Milksync recebe verificações locais recorrentes do áudio original, inclusive no começo e no fim de cada bucket. Isso impede que uma cena exclusiva da HDTV fique escondida entre duas âncoras corretas, logo após uma mudança de bucket ou no meio de um bucket longo. Uma reprovação não libera o dub só porque o restante do bucket parece bom: a pequena faixa reprovada, mais uma margem de segurança, é substituída pelo original do master. Um trecho final comprovadamente compartilhado continua com o dub.

As principais configurações são `tvrip_min_coverage`, `tvrip_min_segment_confidence`, `tvrip_max_segment_seconds`, `tvrip_validation_positions`, `tvrip_fallback`, `tvrip_allow_partial_tracks` e:

```yaml
tvrip:
  tvrip_acoustic_segment_validation: true
  tvrip_acoustic_segment_window_seconds: 5.0
  tvrip_acoustic_segment_min_seconds: 2.0
  tvrip_acoustic_segment_max_gap_seconds: 30.0
  tvrip_acoustic_segment_rejection_padding_seconds: 5.0
  tvrip_acoustic_segment_min_similarity: 0.60
  tvrip_acoustic_segment_require_proof: true
  # Com a TVRip explicitamente autorizada, produz o MKV e registra avisos
  # de cobertura/segmentação para revisão posterior.
  tvrip_continue_on_validation_warnings: true
```

`require_proof` deixa a auditoria local mais exigente. Silêncio nos dois originais não é tratado como corte: não há diálogo ou som a substituir, então o mapeamento já comprovado é preservado. `max_gap_seconds` controla a maior distância entre sondas e também a janela longa usada para revisar uma lacuna aparente do mapa; só quando os dois áudios originais continuam correspondendo nessa janela a dublagem bruta é recuperada. Caso contrário, aplica-se o fallback configurado. `rejection_padding_seconds` amplia o intervalo apontado por uma reprovação. Diminuir o primeiro ou aumentar o segundo é mais conservador, mas também aumenta o tempo de análise.

Com `tvrip_continue_on_validation_warnings: true` — padrão após o opt-in experimental — o mapa completo do Milksync é a autoridade para escolher o áudio: uma janela local com mix, compressão ou música diferente vira aviso no JSON, mas não troca alguns segundos de dublagem pelo inglês. Lacunas reais do próprio mapa continuam usando o fallback configurado. Além disso, dualmaker não repete a comparação visual para cada bucket de um mapa telecinado, pois ela é mais lenta e menos confiável do que os originais já sincronizados. Use `false` (ou `--tvrip-strict-validation`) para a política estrita: nesses casos uma reprovação local pode gerar fallback para o áudio original e as validações completas são mantidas.

O modo interativo mostra os segmentos e permite aceitar, rejeitar, escolher fallback ou cancelar sem publicar um arquivo parcial.

## Montagem do MKV

Na montagem final, o master fornece:

- vídeo, sempre copiado;
- capítulos;
- tags globais selecionadas;
- metadados principais;
- legendas preferidas;
- attachments e fontes, deduplicados por hash.

O stage sincronizado fornece os áudios finais. A montagem especifica a ordem das faixas e as flags de idioma, default, forced e hearing-impaired. O resultado é escrito primeiro com nome parcial. Só depois de uma nova inspeção e validação ele recebe o nome final.

O nome é derivado do master removendo o último grupo de lançamento reconhecido e acrescentando `.DUAL-<tag>.mkv`:

```text
master:  Minions.and.Monsters.2026.1080p.MA.WEB-DL-BYNDR.mkv
saída:   Minions.and.Monsters.2026.1080p.MA.WEB-DL.DUAL-alfaHD.mkv
```

Se o nome já existir, a política padrão cria `.2`, `.3` e assim por diante. `skip` ignora o trabalho e `error` interrompe com erro. Nenhuma fonte é substituída.

Capítulos são copiados do master porque seus pontos representam cenas na linha do tempo do vídeo escolhido. Eles não são reconstruídos a partir da fonte DUAL.

## Configuração

A precedência é:

```text
linha de comando > variáveis DUALMAKER_* > arquivo YAML/TOML > padrões internos
```

O arquivo preferido é:

```text
~/.dualmaker/config.yml
```

O programa cria esse arquivo na primeira execução operacional. Para administrar o arquivo:

```console
dualmaker --init-config
dualmaker --refresh-config
dualmaker --show-config
```

`--refresh-config` preserva os valores existentes, acrescenta as novas configurações documentadas e cria um backup com timestamp. Reinstalar o pacote não é necessário para mudar defaults.

As seções do YAML são:

- `dualmaker`: idioma, seleção de faixas, política de conflito e interface básica;
- `paths`: saída, trabalho, caminhos permitidos e diretórios ignorados;
- `tools`: caminhos de `ffmpeg`, `ffprobe`, MediaInfo e MKVToolNix;
- `security`: grupo do sistema e grupo da saída;
- `features`: recap, duração, gaps, FPS e controles avançados;
- `tvrip`: limites e políticas do fluxo experimental;
- `interface`: formato, cores, progresso, modo silencioso e verbosidade.

Um exemplo completo fica em [dualmaker.example.yml](../dualmaker.example.yml). O programa valida valores no início. Erros comuns incluem binário ausente, caminho fora de `allowed_paths`, diretório sem permissão de escrita, grupo inexistente e thresholds incompatíveis.

## Uso pela linha de comando

Processar o diretório atual:

```console
dualmaker
```

Processar uma pasta:

```console
dualmaker /media/releases
```

Processar recursivamente:

```console
dualmaker /media/series --recursive
```

Ver o plano sem criar MKVs:

```console
dualmaker /media/releases --dry-run
dualmaker /media/releases --json --dry-run
```

Selecionar trabalhos e revisar escolhas:

```console
dualmaker /media/releases --interactive
```

Fornecer um par exato:

```console
dualmaker --dual dual.mkv --normal master.mkv
```

Processar um TVRip explícito:

```console
dualmaker --tvrip broadcast.mkv --normal master.mkv \
  --allow-tvrip-segment-sync --tvrip-fallback original
```

Usar saída e tag próprias:

```console
dualmaker /media/releases \
  --output-dir /media/finalizados \
  --tag MinhaRelease \
  --on-conflict increment
```

Verificar ferramentas externas:

```console
dualmaker --check-deps
```

Para automação, use o modo sem `--interactive`, `--json` e `--report`. O programa retorna `0` quando todos os trabalhos elegíveis terminam, `2` quando existem trabalhos pulados por ambiguidade e `1` para falhas operacionais.

## Interface interativa

O terminal usa uma interface baseada em Rich/Textual. Ela mostra a pasta, os pares completos, os caminhos de cada arquivo, pontuação, faixas escolhidas e destino. A seleção é feita por uma tabela navegável, não por uma sequência de perguntas soltas.

Durante a revisão, é possível selecionar trabalhos, voltar para a etapa anterior, alterar escolhas de áudio ou segmento, confirmar a execução e cancelar. Cancelar não publica saída.

O modo não interativo continua sendo o caminho indicado para scripts. Ele usa os flags e o YAML para decisões determinísticas e registra situações ambíguas sem travar esperando entrada.

## Relatórios e diagnóstico

Cada execução grava um JSON no diretório de saída, salvo quando `--report` define outro caminho. O relatório inclui:

- configuração resolvida e origem de cada valor;
- dependências encontradas;
- metadados completos dos arquivos;
- pares, pontuações e motivos;
- IDs e escolhas de áudio e legenda;
- buckets, atrasos, cortes e pontos de sincronização;
- decisão de recap, FPS e TVRip;
- intervalos somente da fonte e somente do master;
- fallback de dublagem;
- attachments e legendas selecionados;
- duração, identidade do vídeo e validação pós-mux;
- trabalhos pulados e erros.

Quando uma execução falha, procure a primeira causa real no relatório e não apenas a última mensagem do terminal. Avisos como `A/V timeline reconciliation skipped` podem ser diagnósticos auxiliares. Uma falha de validação final, por outro lado, significa que o arquivo não foi publicado como resultado concluído.

Para investigar sem reprocessar tudo, use `--dry-run`, `--show-config`, `--verbose` e `--keep-temp`. O material temporário fica na raiz configurada do projeto ou da mídia. Não altere os arquivos de entrada durante a investigação.

## Dependências externas

O dualmaker chama estes programas do sistema:

- `ffmpeg` para decodificação, probes auxiliares e renderizações necessárias;
- `ffprobe` para tempos e streams;
- `mediainfo` para metadados detalhados;
- `mkvmerge` para identificação e mux final;
- `mkvextract` para recursos Matroska quando necessário;
- `mkvpropedit` para ajustes finais de propriedades.

`--check-deps` mostra o que está disponível e a versão observada. Os caminhos podem ser trocados pelo YAML, pelas variáveis de ambiente ou pelos flags correspondentes.

## API Python

Para integrar o dualmaker em outro programa:

```python
from dualmaker import DualMakerConfig, make_dual, plan_pair, scan_directory

assets = scan_directory("/media/releases", recursive=True)
config = DualMakerConfig(path="/media/releases")
plan = plan_pair(
    "/media/releases/episode.DUAL.mkv",
    "/media/releases/episode.master.mkv",
    config,
)
result = make_dual(plan, config)
```

As funções públicas são:

- `scan_directory()`: inspeciona MKVs elegíveis;
- `plan_pair()`: valida um par e cria um plano determinístico;
- `make_dual()`: executa o plano e retorna um `JobResult`.

Os modelos públicos incluem `MediaAsset`, `Track`, `PairCandidate`, `JobPlan`, `JobResult`, `FPSDecision`, `TVRipSegment` e `TVRipSyncReport`.

## Limites importantes

O vídeo do master é preservado por cópia de stream. O dualmaker não tenta editar o vídeo para inserir cenas exclusivas da TVRip. Por isso, material somente da transmissão pode ser removido do áudio e das legendas, mas não aparece magicamente no vídeo final.

Fontes com FPS diferente, cortes complexos, VobSub com vários atrasos e mapas com pouca evidência continuam sendo casos experimentais. O programa prefere pular ou pedir revisão a publicar um arquivo cuja sincronização não possa ser justificada pelo relatório.

Sidecars externos precisam estar associados ao nome de um dos MKVs. A primeira versão não busca legendas soltas sem relação clara com a dupla.

## Licença e atribuição

O pacote é distribuído sob AGPL-3.0-or-later. A implementação de sincronização mantém a atribuição ao projeto Milksync e inclui os samples de silêncio necessários dentro do pacote, conforme os arquivos de licença e aviso na raiz do projeto.
