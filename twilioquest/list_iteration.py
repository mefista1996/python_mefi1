import sys

# Set up a list for our code to work with that omits the first CLI argument,
# which is the name of our script (list_iteration.py)
order_of_succession = sys.argv
order_of_succession.pop(0)

print("These are the leaders in succession:")
for index, item in enumerate(order_of_succession, start=1):
    string_to_print = f"{index}. {item}"
    print(string_to_print)