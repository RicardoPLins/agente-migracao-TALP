import requests

url = 'http://nettacker.z3r0d4y.com/version.py'

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

# Remove unnecessary blank lines and ensure proper newline at the end of the file
