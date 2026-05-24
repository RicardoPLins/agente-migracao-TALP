
        req = urllib.request.Request(url, requestData, headers)
        with urllib.request.urlopen(req) as response:
            with gzip.GzipFile(fileobj=response) as uncompressed:
                decompressedFile = uncompressed.read()
        end = time.time()
        logging.info("Retrieved in {0:.2f}s".format(end-start))

        #Remove additional leading characters
        msgsData = decompressedFile.decode("utf-8")[9:]
        return  msgsData