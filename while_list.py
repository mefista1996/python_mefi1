#Using while loop, if statement and str() function;
# iterate through the list and if there is a 100, print it with its index number. i.e.: "There is a 100 at index no: 5"

lst = [10, 99, 98, 85, 45, 59, 65, 66, 76, 12, 35, 13, 100, 80, 95]
# Type your code here.

index = 0
while len(lst) >= index:
    index = index + 1
    print(index)
    if lst[index] == 100:
        print('There is number 100 at index:' + str(index))
    else:
        break
