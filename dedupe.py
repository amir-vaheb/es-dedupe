#!/usr/bin/env python
# -*- coding: utf-8 -*-
import datetime
import time
import requests
import ujson
import sys
from io import StringIO
from datetime import timedelta
from time import sleep

def run(args):
    start = time.time()
    print('Using index {0}-{1}'.format(args.prefix, args.index))
    total = 0
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y.%m.%d")
    index = args.index
    while (index != tomorrow):
        while True:
            qs = time.time()
            resp = fetch(index, args)
            qe = time.time()
            docs = 0
            removed = 0
            if isinstance(resp, dict) and ("aggregations" in resp):
                docs = len(resp["aggregations"]["duplicateCount"]["buckets"])
            else:
                print("ERROR: Unexpected response {}".format(resp))
                sys.exit()
            print("ES query took {}, retrieved {} unique docs".format(timedelta(seconds=(qe - qs)),docs))
            if docs > 0:
                removed = remove_duplicates(resp, index, args)
                be = time.time()
                total += removed
                print("Deleted {0} duplicates, in total {1}. Batch processed in {2}, running time {3}".format(removed, total, timedelta(seconds=(be - qe)),timedelta(seconds=(be - start))))
                sleep(1) # avoid flooding ES
            if removed == 0:
                break # continue with next index
        if (not args.all):
            break # process only one index
        index = inc_day(index)
        print('Using index {0}-{1}'.format(args.prefix, index))
    end = time.time()
    print("== de-duplication process completed successfully. Took: {0}".format(timedelta(seconds=(end - start))))

def inc_day(str_date):
    return (datetime.datetime.strptime(str_date, "%Y.%m.%d").date() + datetime.timedelta(days=1)).strftime("%Y.%m.%d")

def es_uri(args):
    return 'http://{0}:{1}'.format(args.host, args.port)

# we have to wait for updating index, otherwise we might be deleting documents that are no longer
# duplicates
def bulk_uri(args):
    return '{0}/_bulk?refresh=wait_for'.format(es_uri(args))

def search_uri(index, args):
    return '{0}/{1}-{2}/_search'.format(es_uri(args), args.prefix, index)

def fetch(index, args):
    uri = search_uri(index, args)
    payload = {"size": 0,
                "aggs":{
                    "duplicateCount":{"terms":
                        {"field": args.field,"min_doc_count": 2,"size":args.batch},
                        "aggs":{
                            "duplicateDocuments":
                                # TODO: _source can contain custom fields, when empty whole document is trasferred
                                # which causes unnecessary traffic
                                {"top_hits":{"size": args.dupes, "_source":[args.field]}}
                          }
                        }
                }
            }
    try:
        json = ujson.dumps(payload)
        if args.verbose:
            print("POST {0}".format(uri))
            print("\tdata: {0}".format(json))
        resp = requests.post(uri, data=json)
        if args.debug:
            print("resp: {0}".format(resp.text))
        if resp.status_code == 200:
            r = ujson.loads(resp.text)
            return r
        else:
            print("failed to fetch duplicates: #{0}".format(resp.text))
    except requests.exceptions.ConnectionError as e:
        print("ERROR: connection failed, check --host argument and port. Is ES running on {0}?".format(es_uri(args)))
        print(e)
    return 0

def remove_duplicates(json, index, args):
    docs = []
    ids = []
    for bucket in json["aggregations"]["duplicateCount"]["buckets"]:
        docs.append("{0}:{1}-{2}/{3}/{4}".format(bucket['key'], args.prefix, index, args.doc_type, bucket["duplicateDocuments"]["hits"]["hits"][0]["_id"]))
        #print("bucket: {0}".format(bucket))
        i = 0
        for dupl in bucket["duplicateDocuments"]["hits"]["hits"]:
            if i > 0:
                ids.append(dupl["_id"])
            else:
                if args.verbose:
                    print("skipping doc {0}".format(dupl["_id"]))
            i += 1
    removed = bulk_remove(ids, index, args)
    with open(args.docs_log, mode='a', encoding='utf-8') as f:
        f.write('\n'.join(docs))
        f.write('\n')
    return removed

# returns number of deleted items
def bulk_remove(ids, index, args):
    buf = StringIO()
    for i in ids:
        buf.write('{"delete":{"_index":"')
        buf.write(args.prefix)
        buf.write('-')
        buf.write(index)
        buf.write('","_type":"')
        buf.write(args.doc_type)
        buf.write('","_id":"')
        buf.write(i)
        buf.write('"}}\n')
    try:
        uri = bulk_uri(args)
        if args.verbose:
            print("POST {}".format(uri))
        resp = requests.post(uri, data=buf.getvalue())
        if args.debug:
            print("resp: {0}".format(resp.text))
        if resp.status_code == 200:
            r = ujson.loads(resp.text)
            if r['errors']:
                print(r)
            cnt = 0
            for item in r['items']:
                if ('found' in item['delete']) and item['delete']['found'] == True:
                    cnt += 1
                else:
                    print(item)
            return cnt
        else:
            print("failed to fetch duplicates: #{0}".format(resp.text))
    except requests.exceptions.ConnectionError as e:
        print("ERROR: connection failed, check --host argument and port. Is ES running on {0}?".format(es_uri(args)))
        print(e)

    buf.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Elasticsearch deduplicator")
    parser.add_argument("-a", "--all",
                        action="store_true", dest="all",
                        default=False,
                        help="All indexes from given date till today")
    parser.add_argument("-b","--batch",
                        dest="batch", default=10, type=int,
                        help="Batch size - how many documents are retrieved using one GET request")
    parser.add_argument("-m","--max_dupes",
                        dest="dupes", default=10, type=int,
                        help="Dupes size - how many duplicates per document are retrieved")
    parser.add_argument("-H", "--host", dest="host",
                        default="localhost",
                        help="Elasticsearch hostname", metavar="host")
    parser.add_argument("-f", "--field", dest="field",
                        default="Uuid",
                        help="Field in ES that suppose to be unique", metavar="field")
    parser.add_argument("-i", "--index", dest="index",
                        default=datetime.date.today().strftime("%Y.%m.%d"),
                        help="Elasticsearch index suffix", metavar="index")
    parser.add_argument("-p", "--prefix", dest="prefix",
                        default="nginx_access_logs",
                        help="Elasticsearch index prefix", metavar="prefix")
    parser.add_argument("-P", "--port", dest="port",
                        default=9200, type=int,
                        help="Elasticsearch pord", metavar="port")
    parser.add_argument("-t", "--doc_type", dest="doc_type",
                        default="nginx.access",
                        help="ES doctype")
    parser.add_argument("-v", "--verbose",
                        action="store_true", dest="verbose",
                        default=False,
                        help="enable verbose logging")
    parser.add_argument("-d", "--debug",
                        action="store_true", dest="debug",
                        default=False,
                        help="enable debugging")
    parser.add_argument("--docs_log", dest="docs_log",
                        default="/tmp/es_dedupe.log",
                        help="Logfile for processed documents")


    args = parser.parse_args()
    print("== Starting ES deduplicator....")
    if args.verbose:
        print(args)
    try:
        run(args)
    except KeyboardInterrupt:
        print('Interrupted')
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)
