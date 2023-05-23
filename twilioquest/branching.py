import sys

var1 = int(sys.argv[1])
var2 = int(sys.argv[2])
sum = var1 + var2
if sum < 0 or sum == 0:
    print("You have chosen the path of destitution.")
elif sum in range(1,101):
    print("You have chosen the path of plenty.")
elif sum > 100:
    print("You have chosen the path of excess.")
else:
    print("Well, try again")
