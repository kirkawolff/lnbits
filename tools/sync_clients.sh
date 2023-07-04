#!/bin/sh
for client_dir in $(find clients -mindepth 1 -maxdepth 1 -type d) ; do
    cd $client_dir
    client_repo=$(echo $client_dir | sed "s|clients/|client-|g")
    git init
    git remote add origin git@github.com:dni/lnbits-$client_repo
    git checkout -b main
    git add -A
    git commit -am "update client"
    git push origin main --force
    cd ../..
done
