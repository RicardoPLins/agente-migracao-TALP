 	
        response = requests.post(url, data=requestData, headers=headers)



        end = time.time()
        logging.info("Retrieved in {0:.2f}s".format(end-start))

        #Remove additional leading characters
        msgsData = response.text[9:]
        return  msgsData