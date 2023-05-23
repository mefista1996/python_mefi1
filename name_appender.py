# Using while loop and an if statement write a function named name_adder which appends all the elements in a
# list to a new list unless the element is an empty string: "".


lst1 = ["Joe", "Sarah", "Mike", "Jess", "", "Matt", "", "Greg"]

name_added = input('Please enter your name: ')
def name_adder(name):
    name = name_added
    while len(name) > 0:
        lst1.append(name)
        print(lst1)
        len(name) + 1
        if name == "":
            print("No way")



