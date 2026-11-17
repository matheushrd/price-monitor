# Monitor de preços — Poco X3

Todo dia o celular visita o link de cada produto, grava o preço no PostgreSQL e atualiza a
planilha "Monitoramento Diário de Preços — Eletrodomésticos 127V" no formato que você já usa
(uma coluna por dia + Preço Atual).

```
cron (Termux) -> run.sh -> collect.py ──> PostgreSQL ──> sheet_sync.py ──> Google Sheets
                                (preços)    (histórico)    (coluna do dia)
```

## 1. Conta de serviço do Google (uma vez, ~10 min)

É o "usuário robô" que tem permissão para editar a planilha.

1. Abra https://console.cloud.google.com e crie um projeto (ex.: `monitor-precos`).
2. Menu **APIs e serviços → Biblioteca** → procure **Google Sheets API** → **Ativar**.
3. **IAM e administrador → Contas de serviço → Criar conta de serviço** (nome livre, sem papéis).
4. Abra a conta criada → aba **Chaves** → **Adicionar chave → Criar nova chave → JSON**. Um arquivo é baixado.
5. Copie o e-mail da conta (termina em `.iam.gserviceaccount.com`), abra a planilha →
   **Compartilhar** → cole o e-mail → permissão **Editor**.
6. Renomeie o arquivo baixado para `credentials.json`.

Faça uma **cópia de segurança da planilha** (Arquivo → Fazer uma cópia) antes da primeira gravação real.

## 2. Levar os arquivos para o celular

No PC (PowerShell), na pasta onde estão `price-monitor.zip` e `credentials.json`:

```powershell
scp -P 8022 price-monitor.zip credentials.json u0_a262@poco-x3-nfc:~/
```

No Termux (via SSH):

```bash
pkg install -y unzip
ROOTFS=$PREFIX/var/lib/proot-distro/installed-rootfs/debian
unzip -o ~/price-monitor.zip -d $ROOTFS/home/matheus/
mv ~/credentials.json $ROOTFS/home/matheus/price-monitor/
chmod 600 $ROOTFS/home/matheus/price-monitor/credentials.json
```

## 3. Instalar o PostgreSQL (no Termux) e o Python (no Debian)

O `proot` não consegue rodar o PostgreSQL (erro "wrong ownership"), então o banco roda **direto no
Termux** e o Python continua no Debian, conectando por `127.0.0.1:5432`.

No **Termux**:

```bash
pkg install -y postgresql
mkdir -p $PREFIX/var/lib/postgresql
initdb -E UTF8 --locale=C --auth-local=trust --auth-host=scram-sha-256 $PREFIX/var/lib/postgresql
pg_ctl -D $PREFIX/var/lib/postgresql -l $HOME/postgres.log start
psql -d postgres -c "CREATE ROLE pricemon LOGIN PASSWORD 'SUA_SENHA'"
createdb -O pricemon pricemon
```

Depois, no **Debian** como `matheus` (`proot-distro login debian --user matheus`), crie o `.env`
com a mesma senha e rode a instalação do Python:

```bash
nano ~/price-monitor/.env     # conteúdo abaixo
chmod 600 ~/price-monitor/.env
bash ~/price-monitor/setup_python.sh
```

Conteúdo do `.env` (troque `SUA_SENHA`):

```
DATABASE_URL=postgresql://pricemon:SUA_SENHA@127.0.0.1:5432/pricemon
SHEET_ID=1D1jZUfrecx6FA4qtnS_I3teeKddcBsTOeuYma20ucF4
SHEET_TAB=Monitoramento
GOOGLE_CREDENTIALS=/home/matheus/price-monitor/credentials.json
```

O histórico da planilha (28 produtos, 15/10 a 02/11) é importado do `Monitoramento.csv`.

## 4. Primeiro teste (importante)

```bash
cd ~/price-monitor && . .venv/bin/activate
python collect.py --dry-run      # lê as lojas e mostra o que acharia, sem gravar nada
```

Compare a coluna do preço lido com a coluna "antes". Cada linha termina em um status:

| status | significado |
|---|---|
| `ok` | preço lido e coerente com o último |
| `suspeito` | variou mais de 35% de um dia para o outro; **não entra na planilha** até você aprovar |
| `erro` | HTTP 403/404, bloqueio ou página sem preço |
| `indisponivel` | a página informa que está esgotado |

Se uma loja der `erro` ou o preço vier diferente do que aparece no site (Pix × tabela), ajuste
`STORE_RULES` no topo de `extract.py` (há exemplos comentados): `pix_regex` pega o preço exato
por expressão regular, e `pick` escolhe `min`/`max`/`first` entre as ofertas da página.

Depois: `python collect.py`, em seguida `python sheet_sync.py --dry-run` (mostra o que mudaria na
planilha) e, por fim, `python sheet_sync.py`.

## 5. Automatizar (Termux)

Adicione o PostgreSQL e o cron ao script de boot e instale o cron:

```bash
pkg install -y cronie
nano ~/.termux/boot/start-services
```

No final do arquivo (depois de `sshd`), acrescente estas linhas:

```
pg_ctl -D /data/data/com.termux/files/usr/var/lib/postgresql -l /data/data/com.termux/files/home/postgres.log start
sleep 5
crond
```

Depois `crontab -e` e cole (todo dia às 08:10, horário de Brasília):

```
CRON_TZ=America/Sao_Paulo
10 8 * * * /data/data/com.termux/files/usr/bin/proot-distro login debian --user matheus -- bash /home/matheus/price-monitor/run.sh >> $HOME/price-monitor.log 2>&1
```

Para subir o cron agora sem reiniciar, rode `crond` (o PostgreSQL já foi iniciado no passo 3).
O log fica em `~/price-monitor.log`. Para testar a rotina inteira: `bash ~/price-monitor/run.sh` dentro do Debian.

## 6. No dia a dia

- **Produto marcado "Verificar"** na planilha: confira no site; se o preço novo é verdadeiro,
  `python collect.py --approve ID` (o ID aparece na saída do coletor) e depois `python sheet_sync.py`.
- **Produto novo**: adicione a linha na planilha com o Link Direto. Entra no banco na próxima rodada.
- **Como a planilha muda**: a coluna "Preço Atual em ..." do dia anterior é copiada para uma coluna
  de data nova logo antes dela, e "Preço Atual" passa a mostrar hoje. Status Diário vira Caiu / Subiu /
  Estável (ou Verificar / Indisponível / Falha na coleta). Loja, Link Direto e Observação não são tocados.
  Colunas que forem fórmulas na planilha são respeitadas.
- **Histórico no banco**: `psql -d pricemon` (no Termux) e consulte `price_snapshots`.
- **Backup**: `pg_dump pricemon > backup.sql` (no Termux), de vez em quando.

## Limites

- A leitura de preços **não foi testada nas lojas reais** (de onde eu estava não dava para acessá-las).
  Magalu, Casas Bahia e Mercado Livre costumam ter proteção anti-robô; o `--dry-run` mostra quais passam.
- Depois de **reiniciar o celular**, é preciso desbloqueá-lo uma vez para o Termux liberar os arquivos.
- O coletor visita cada link uma vez por dia, com pausa aleatória entre lojas.
