import requests

def _update():
    from my_module import version
    
    if version() == 3:
        try:
            response = requests.get(url)
            # Ensure we handle HTTP errors
            response.raise_for_status()
            data = response.text
        except requests.exceptions.RequestException as e:
            print(f"Error fetching URL: {e}")
    else:
        pass

def _check():
    from my_module import version
    
    if version() == 3:
        try:
            response = requests.get(url)
            # Ensure we handle HTTP errors
            response.raise_for_status()
            data = response.text
        except requests.exceptions.RequestException as e:
            print(f"Error fetching URL: {e}")
    else:
        pass

# Example usage
_update()
_check()
