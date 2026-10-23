# Git workflow for Mac Raspberry and Tilik

Satu sumber runtime berada di https://github.com/Mweh/tilik-edge. Repo Tilik private memakai repo public ini sebagai submodule pada `edge/tilik-edge`. Repo public menyimpan kode dan contoh konfigurasi; data capture, token, database, serta konfigurasi perangkat lokal tidak masuk Git.

## Mac dalam repo Tilik

Clone baru:

```sh
git clone --recurse-submodules https://github.com/Mweh/Tilik.git
```

Checkout Tilik yang sudah ada:

```sh
git submodule update --init --recursive
```

Untuk mengubah runtime:

```sh
cd edge/tilik-edge
git switch main
git pull --ff-only
# Edit kode lalu uji
python3 -m unittest discover -s tests -v
git add <file-yang-diubah>
git commit -m "fix: describe the edge change"
git push origin main
```

Setelah commit edge dipush, commit pointer submodule pada branch kerja repo Tilik. Perubahan iOS lain tetap terpisah; jangan commit semuanya secara tidak sengaja.

```sh
cd ../..
git add edge/tilik-edge
git commit -m "chore(edge): update thermal runtime"
```

Untuk mengambil versi yang dipin Tilik: `git submodule update --init --recursive`. Untuk mengambil main edge terbaru secara sengaja: `git submodule update --remote --merge edge/tilik-edge`, lalu review dan commit pointer baru. Git tidak menyamakan perangkat secara otomatis sebelum pull; hash commit adalah bukti versi yang sama.

## Sambungkan instalasi Pi yang sudah ada

Hentikan `start.sh` dengan Ctrl-C. Script `deploy/connect-git.sh` harus tersedia di Pi. Jalankan:

```sh
cd ~/tilik-edge
sh deploy/connect-git.sh
sh start.sh
```

Script menyimpan backup kode/config, menyalin setting kamera yang sekarang ke `config/pi.local.json`, dan aturan ke `config/rules.local.json`, lalu mengambil kode Git main. Data capture dan katalog tidak dihapus atau dipindah. `start.sh` memilih pi.local.json secara otomatis. Periksa output import sukses sebelum menyalakan kamera.

Jika Git fetch gagal karena koneksi, jangan menghapus data atau menjalankan reset hard. Backup tetap tersedia. Setelah koneksi pulih, periksa `git remote -v`, lalu jalankan fetch dan restore tracked files sesuai kebutuhan. Script tidak memodifikasi ulang folder yang sudah memiliki `.git`.

## Update rutin Pi

```sh
cd ~/tilik-edge
# Hentikan start.sh sebelum memperbarui kode
git status --short
git pull --ff-only
python3 -m unittest discover -s tests -v
sh start.sh
```

Jika pull ditolak karena ada perubahan lokal, simpan perubahan sebagai commit/branch atau pindahkan setting ke pi.local.json; jangan memakai reset hard untuk melewati peringatan. Pada Pi gunakan pull-only untuk operasi normal; commit kode sebaiknya dibuat di Mac.

Bandingkan versi pada Mac dan Pi:

```sh
git rev-parse HEAD
```

Hash yang sama berarti versi kode Git sama. Konfigurasi dan capture memang berbeda per perangkat. Untuk memakai commit edge yang dipin Tilik, jalankan `git switch --detach <hash>` pada Pi. Kembali ke main dengan `git switch main`.

## Instalasi Pi baru

```sh
git clone https://github.com/Mweh/tilik-edge.git ~/tilik-edge
cd ~/tilik-edge
cp config/payload.json config/pi.local.json
# Sesuaikan node kamera dan konfirmasi radiometri pada file lokal
sh start.sh
```

Public clone tidak meminta password GitHub. Push hanya untuk akun yang mendapat hak tulis. Password SSH Pi diisi langsung di terminal; password/token tidak disimpan dalam repo. GitHub menyimpan riwayat kode, sedangkan backup capture memerlukan penyimpanan terpisah.
